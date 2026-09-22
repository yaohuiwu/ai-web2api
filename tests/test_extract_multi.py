"""多容器拼接提取（response_all_new）：Kimi 一次回答可能拆成多条消息。

运行：.venv/bin/python -m pytest tests/test_extract_multi.py -v
"""

from __future__ import annotations

import pytest

from ai_web2api.browser import extractor


@pytest.fixture
def fake_page(monkeypatch):
    """用假的 count/extract 模拟 N 个容器。"""
    parts = ["好呀，我用代码给你画一只小兔子 🐰", "画好啦，一只粉色长耳朵小兔子 🐰", "想调整的话随时说，比如：换成白色兔子。"]
    calls: list[int] = []

    async def fake_count(page, sel):
        return len(parts)

    async def fake_extract(page, sel, index=-1):
        calls.append(index)
        i = -1 if index is None else index
        return parts[i] if -1 <= i < len(parts) else ""

    monkeypatch.setattr(extractor, "count_matches", fake_count)
    monkeypatch.setattr(extractor, "extract_markdown", fake_extract)
    return parts, calls


@pytest.mark.asyncio
async def test_extract_all_from_start(fake_page):
    parts, calls = fake_page
    out = await extractor.extract_markdown_from(None, ".md", 0)
    assert out.split("\n\n") == parts            # 全部拼接，顺序保持
    assert calls == [0, 1, 2]


@pytest.mark.asyncio
async def test_extract_from_middle(fake_page):
    parts, calls = fake_page
    out = await extractor.extract_markdown_from(None, ".md", 1)
    assert out.split("\n\n") == parts[1:]
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_start_beyond_count_and_empty_selector(fake_page):
    assert await extractor.extract_markdown_from(None, ".md", 99) == ""
    assert await extractor.extract_markdown_from(None, "", 0) == ""
    assert await extractor.extract_markdown_from(None, None, 0) == ""


@pytest.mark.asyncio
async def test_adjacent_duplicates_deduped(monkeypatch):
    """容器重渲染出现相邻重复内容时去重（避免正文重复输出）。"""
    parts = ["A", "A", "B"]

    async def fake_count(page, sel):
        return len(parts)

    async def fake_extract(page, sel, index=-1):
        return parts[index]

    monkeypatch.setattr(extractor, "count_matches", fake_count)
    monkeypatch.setattr(extractor, "extract_markdown", fake_extract)
    assert (await extractor.extract_markdown_from(None, ".md", 0)).split("\n\n") == ["A", "B"]


@pytest.mark.asyncio
async def test_provider_picks_join_mode_by_config(monkeypatch):
    """_extract_content：response_all_new=true 才拼接，否则维持"只取该下标"。"""
    from ai_web2api.config import ProviderConfig
    from ai_web2api.providers.webchat import WebChatProvider

    class _StubBrowser:
        default_locale = "zh-CN"

    seen: list[str] = []

    async def single(page, sel, index=-1):
        seen.append("single")
        return "single"

    async def joined_parts(page, sel, start):
        seen.append("joined")
        return ["joined"]

    monkeypatch.setattr(extractor, "extract_markdown", single)
    monkeypatch.setattr(extractor, "extract_markdown_parts", joined_parts)

    def _prov(**sel):
        cfg = ProviderConfig.model_validate(
            {"name": "x", "url": "https://x/", "models": [{"name": "m"}], "selectors": sel}
        )
        return WebChatProvider(cfg, _StubBrowser())  # type: ignore[arg-type]

    assert await _prov()._extract_content(None, ".md", 0) == "single"
    assert await _prov(response_all_new=True)._extract_content(None, ".md", 0) == "joined"
    assert seen == ["single", "joined"]
    # 没有容器/下标时直接空串，不去调页面
    assert await _prov(response_all_new=True)._extract_content(None, None, None) == ""


def test_kimi_enables_join_mode():
    from pathlib import Path

    from ai_web2api.config import load_config

    root = Path(__file__).resolve().parent.parent
    kimi = next(p for p in load_config(root / "config.yaml").providers if p.name == "kimi")
    assert kimi.selectors.response_all_new is True
    # 路线 1：**启用的** provider 一律打开拼接（防"多容器回答被截断"）
    providers = {p.name: p for p in load_config(root / "config.yaml").providers}
    for name in ("deepseek", "chatgpt", "doubao"):
        assert providers[name].selectors.response_all_new is True, f"{name} 应打开 response_all_new"


def test_join_mode_filters_thinking_blocks(monkeypatch):
    """拼接多容器时，按**内容**剔除"其实是思考"的块（下标错位也能挡住）。"""
    import asyncio

    from ai_web2api.config import ProviderConfig
    from ai_web2api.providers.webchat import WebChatProvider

    class _StubBrowser:
        default_locale = "zh-CN"

    cfg = ProviderConfig.model_validate(
        {
            "name": "kimi",
            "url": "https://www.kimi.com/",
            "models": [{"name": "kimi-web"}],
            "selectors": {"response_all_new": True},
        }
    )
    prov = WebChatProvider(cfg, _StubBrowser())  # type: ignore[arg-type]

    async def parts(_page, _sel, _start):
        return [
            'The user says "再给生成一次". I will call showwidget again.',   # = 思考，应剔除
            "路线数据已确认。现在把这套环线做成可交互的地图规划。",               # 过程性文本，保留
            "已重新生成，内容与上一版一致：点 Day 1-4 标签切换每日行程。",          # 正文
        ]

    monkeypatch.setattr(extractor, "extract_markdown_parts", parts)
    thinking = 'The user says "再给生成一次". I will call showwidget again. I will render it again.'
    out = asyncio.run(prov._extract_content(None, ".md", 0, thinking))
    assert "The user says" not in out, out
    assert "已重新生成" in out and "路线数据已确认" in out


def test_looks_like_thinking_helper():
    from ai_web2api.providers.webchat import WebChatProvider

    think = "我们需要先确认目的地和天数，再给出路线。"
    assert WebChatProvider._looks_like_thinking(think, think) is True
    assert WebChatProvider._looks_like_thinking("我们需要先确认目的地", think) is True   # 子串
    assert WebChatProvider._looks_like_thinking("已重新生成，内容与上一版一致", think) is False
    assert WebChatProvider._looks_like_thinking("任意正文", "") is False


# ---------- 交互组件（iframe）：中间结果不能完全丢 ----------


class _BodyLoc:
    def __init__(self, text, fail=False):
        self._text = text
        self._fail = fail

    async def inner_text(self, timeout=0):
        if self._fail:
            raise RuntimeError("frame detached")
        return self._text


class _Frame:
    def __init__(self, url, text="", fail=False):
        self.url = url
        self._loc = _BodyLoc(text, fail)

    def locator(self, _sel):
        return self._loc


class _FramePage:
    def __init__(self, frames):
        self.frames = frames


def _kimi_prov(**selectors):
    from ai_web2api.config import ProviderConfig
    from ai_web2api.providers.webchat import WebChatProvider

    class _StubBrowser:
        default_locale = "zh-CN"

    cfg = ProviderConfig.model_validate(
        {
            "name": "kimi",
            "url": "https://www.kimi.com/",
            "models": [{"name": "kimi-web"}],
            "selectors": selectors,
        }
    )
    return WebChatProvider(cfg, _StubBrowser())  # type: ignore[arg-type]


def test_frame_results_adds_new_widget():
    """本轮新出现的 iframe（Kimi 地图组件）→ 链接 + 可见文本。"""
    import asyncio

    prov = _kimi_prov(include_frames=True)
    page = _FramePage(
        [
            _Frame("https://old.kimi-canvas.com/", "旧组件"),          # 发送前就有 → 跳过
            _Frame("https://new.kimi-canvas.com/", "Day1 洛阳 Day2 登封"),
            _Frame("about:blank"),
        ]
    )
    out = asyncio.run(prov._frame_results(page, {"https://old.kimi-canvas.com/"}))
    assert "new.kimi-canvas.com" in out and "Day1 洛阳" in out
    assert "旧组件" not in out


def test_frame_results_disabled_by_default():
    import asyncio

    prov = _kimi_prov()
    page = _FramePage([_Frame("https://x.kimi-canvas.com/", "内容")])
    assert asyncio.run(prov._frame_results(page, set())) == ""


def test_frame_results_tolerates_text_failure():
    import asyncio

    prov = _kimi_prov(include_frames=True)
    page = _FramePage([_Frame("https://y.kimi-canvas.com/", fail=True)])
    out = asyncio.run(prov._frame_results(page, set()))
    assert "y.kimi-canvas.com" in out, "读不到文本也要给出链接"
