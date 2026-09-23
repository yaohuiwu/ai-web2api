"""结束判定与超时兜底：思考持续跳动不该拖死正文；超时不该丢掉已生成的内容。

回归背景（实测 Kimi）：思考区有"正在思考中 Ns"这类**持续变化**的文本，
若 content/thinking 共用稳定计数 → 永远判不出结束 → 180s 超时，而画面上答案其实已生成。
运行：.venv/bin/python -m pytest tests/test_response_stability.py -v
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ai_web2api.browser import extractor
from ai_web2api.config import ProviderConfig
from ai_web2api.providers.webchat import WebChatProvider


class _StubBrowser:
    default_locale = "zh-CN"


class _NoLoc:
    @property
    def first(self):
        return self

    async def is_visible(self):
        return False


class _FakePage:
    def __init__(self) -> None:
        self.clock = 0.0

    async def wait_for_timeout(self, ms: float) -> None:
        await asyncio.sleep(ms / 1000)
        self.clock += ms / 1000

    def locator(self, _sel):
        return _NoLoc()


def _prov(**selectors) -> WebChatProvider:
    cfg = ProviderConfig.model_validate(
        {
            "name": "kimi",
            "url": "https://www.kimi.com/",
            "models": [{"name": "kimi-web"}],
            "selectors": selectors,
            "poll_interval": 0.01,
            "stable_polls": 3,
            "min_wait_before_stable": 0.0,
            "response_timeout": 1.0,
            "send_confirm_timeout": 0.05,   # 测试里尽快走到"确认/重发"分支
        }
    )
    return WebChatProvider(cfg, _StubBrowser())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_thinking_churn_does_not_block_completion(monkeypatch):
    """思考一直变、正文几拍后稳定 → 必须能结束并产出正文（而不是超时）。"""
    prov = _prov(response_container=[".md"], thinking_container=[".th"])
    page = _FakePage()
    state = {"polls": 0}

    async def fake_count(_page, sel):
        return 1 if sel == ".md" else 0

    async def fake_extract(_page, sel, index=-1):
        state["polls"] += 1
        if sel != ".md":
            return ""
        return "答案" if state["polls"] >= 3 else ""      # 前几拍还没出正文

    async def fake_thinking(self, _page, sel, idx):
        return f"正在思考中 {state['polls']}s"             # 每拍都变（跳动）

    monkeypatch.setattr(extractor, "count_matches", fake_count)
    monkeypatch.setattr(extractor, "extract_markdown", fake_extract)
    monkeypatch.setattr(WebChatProvider, "_extract_thinking", fake_thinking)

    chunks = [c async for c in prov._poll_response_dom(page, {".md": 0}, {".th": 0})]
    assert any(c.text == "答案" for c in chunks if c.kind == "content"), chunks
    assert state["polls"] < 200, "应在稳定若干拍后结束，而不是一直轮询到超时"


@pytest.mark.asyncio
async def test_timeout_returns_content_instead_of_error(monkeypatch):
    """超时兜底：内容其实是"迟到"生成的 → 返回它，而不是报 180s 超时把答案丢掉。"""
    prov = _prov(response_container=[".md"], thinking_container=[])
    page = _FakePage()
    t0 = time.monotonic()

    async def fake_count(_page, _sel):
        return 1

    async def fake_extract(_page, _sel, index=-1):
        # 前 0.2s 拿不到（模拟渲染慢/容器错位），之后才拿到
        return "迟到的答案" if time.monotonic() - t0 > 0.2 else ""

    monkeypatch.setattr(extractor, "count_matches", fake_count)
    monkeypatch.setattr(extractor, "extract_markdown", fake_extract)

    chunks = [c async for c in prov._poll_response_dom(page, {".md": 0}, {})]
    assert any("迟到的答案" in (c.text or "") for c in chunks), chunks


@pytest.mark.asyncio
async def test_timeout_with_nothing_still_errors(monkeypatch):
    """真的什么都没有 → 仍然报超时（不能把空当成功）。"""
    from ai_web2api.core.errors import ResponseTimeoutError

    prov = _prov(response_container=[".md"], thinking_container=[])
    page = _FakePage()

    async def fake_count(_page, _sel):
        return 1

    async def fake_extract(_page, _sel, index=-1):
        return ""

    monkeypatch.setattr(extractor, "count_matches", fake_count)
    monkeypatch.setattr(extractor, "extract_markdown", fake_extract)

    with pytest.raises(ResponseTimeoutError):
        [c async for c in prov._poll_response_dom(page, {".md": 0}, {})]


@pytest.mark.asyncio
async def test_busy_hint_dismisses_then_resends(monkeypatch):
    """繁忙弹窗会挡住发送（实测：文字留在输入框）→ 必须先关弹窗再**重发**，而不是直接报错。"""
    from ai_web2api.browser import extractor as ex

    prov = _prov(
        response_container=[".md"], thinking_container=[], stop_button=["button.stop"],
        dismiss_button=["button.dismiss"],
        busy_hint=[".modal:has-text('优先队列')"],
    )
    page = _FakePage()
    sent: list[str] = []
    counts = {"n": 1}          # 发送前 1 个容器，发送后不增加（模拟"没发出去"）
    state = {"gen_after_resend": True}

    # 注意：初次发送发生在 send()（不在本函数内），_poll_response_dom 里的 sent 只记"重发"
    async def fake_count(_page, sel):
        if sel == ".md" and len(sent) >= 1:      # 关掉弹窗、重发后才开始生成
            return counts["n"] + 1
        return counts["n"]

    async def fake_extract(_page, _sel, index=-1):
        return "答案" if len(sent) >= 1 else ""

    async def fake_send(self, _page, _sel, prompt):
        sent.append(prompt)

    async def fake_dismiss(self, _page, reason=""):
        return ["button.dismiss"]

    async def fake_busy(self, _page):
        return len(sent) < 1                     # 还没重发时，弹窗在（挡住发送）

    monkeypatch.setattr(ex, "count_matches", fake_count)
    monkeypatch.setattr(ex, "extract_markdown", fake_extract)
    monkeypatch.setattr(type(prov), "_send_prompt", fake_send)
    monkeypatch.setattr(type(prov), "_dismiss_overlays", fake_dismiss)
    monkeypatch.setattr(type(prov), "_busy_hint_visible", fake_busy)

    chunks = [
        c async for c in prov._poll_response_dom(
            page, {".md": 1}, {}, input_sel=".editor", prompt="hi"
        )
    ]
    assert len(sent) == 1, f"繁忙提示后必须**重发一次**（实际重发 {len(sent)} 次）"
    assert any("答案" in (c.text or "") for c in chunks)
    assert state["gen_after_resend"] is True


@pytest.mark.asyncio
async def test_stop_button_requires_settle_before_done(monkeypatch):
    """停止按钮消失后必须**再等 N 拍无变化**才定稿（否则最后一拍渲染未完成 → 截断）。"""
    from ai_web2api.browser import extractor as ex
    from ai_web2api.providers.webchat import WebChatProvider

    prov = _prov(response_container=[".md"], thinking_container=[], stop_button=["button.stop"],
                 stop_settle_polls=4)
    page = _FakePage()
    polls = {"n": 0}
    stop_vis = {"n": 0}

    async def fake_count(_p, _sel):
        return 1

    async def fake_extract(_p, _sel, index=-1):
        polls["n"] += 1
        return "答案"

    async def fake_visible(_p, _sel):
        stop_vis["n"] += 1
        return stop_vis["n"] <= 1          # 第 1 拍可见，之后消失

    monkeypatch.setattr(ex, "count_matches", fake_count)
    monkeypatch.setattr(ex, "extract_markdown", fake_extract)
    monkeypatch.setattr(WebChatProvider, "_is_visible", staticmethod(fake_visible))

    chunks = [c async for c in prov._poll_response_dom(page, {".md": 0}, {})]
    assert any("答案" in (c.text or "") for c in chunks)
    # 停止按钮消失后还要稳定 3 拍 → 至少多轮询几次（不是一消失就返回）
    assert stop_vis["n"] >= 4, (
        f"停止按钮消失后应按配置(stop_settle_polls=4)继续确认稳定，"
        f"实际只调了 {stop_vis['n']} 次（配置没被读到？）"
    )


@pytest.mark.asyncio
async def test_preview_stream_emits_before_finalize(monkeypatch):
    """预览流：缓冲模式下**一有新增就发**，用户不必等定稿（实测定稿要再等 ~8s）。

    注意不能要求"文本稳定 N 拍后才发"——站点连续吐字时每拍都在变（实测豆包每 0.2s 长一次），
    等稳定等于"等到停顿才发"，用户看到的就是"缓冲/一次性返回"。
    """
    from ai_web2api.browser import extractor as ex

    prov = _prov(response_container=[".md"], thinking_container=[], stream_content=False,
                 preview_stream=True, preview_min_chars=1)
    page = _FakePage()
    seq = ["第一段。", "第一段。第二段。", "第一段。第二段。第三段。"]
    polls = {"n": 0}

    async def fake_count(_p, _sel):
        return 1

    async def fake_extract(_p, _sel, index=-1):
        i = min(polls["n"], len(seq) - 1)
        polls["n"] += 1
        return seq[i]

    monkeypatch.setattr(ex, "count_matches", fake_count)
    monkeypatch.setattr(ex, "extract_markdown", fake_extract)

    chunks: list[tuple[str, str]] = []
    first_at: int | None = None
    async for c in prov._poll_response_dom(page, {".md": 0}, {}):
        if first_at is None and c.kind == "content":
            first_at = polls["n"]          # 第几次取文本时发出了第一个增量
        chunks.append((c.kind, c.text))
    text = "".join(t for k, t in chunks if k == "content")
    assert text == seq[-1], f"拼接后必须与定稿完全一致（无重复无丢失），实际：{text!r}"
    assert first_at is not None
    assert first_at < polls["n"] - 2, (
        f"预览流必须**在定稿前**发出（首次 t=第{first_at}拍 / 定稿在第{polls['n']}拍）"
    )
    assert first_at <= 2, f"攒够 preview_min_chars 就该发（不该等稳定），实际第 {first_at} 拍"
    assert len(chunks) >= 3, f"应边生成边发多条增量，实际 {len(chunks)} 条"


@pytest.mark.asyncio
async def test_buffered_without_preview_keeps_old_behavior(monkeypatch):
    """preview_stream=false（默认）→ 与旧行为一致：定稿时一次性发完整正文。"""
    from ai_web2api.browser import extractor as ex

    prov = _prov(response_container=[".md"], thinking_container=[], stream_content=False)
    page = _FakePage()
    state = {"n": 0}

    async def fake_count(_p, _sel):
        return 1

    async def fake_extract(_p, _sel, index=-1):
        state["n"] += 1
        return "完整正文" if state["n"] > 1 else "完"

    monkeypatch.setattr(ex, "count_matches", fake_count)
    monkeypatch.setattr(ex, "extract_markdown", fake_extract)

    chunks = [(c.kind, c.text) async for c in prov._poll_response_dom(page, {".md": 0}, {})]
    contents = [t for k, t in chunks if k == "content"]
    assert contents == ["完整正文"], f"应只发一次完整正文，实际 {contents}"


def test_suffix_after_handles_divergence():
    """预览流与定稿分歧（重渲染改写）时，从分歧点补发，尽量不丢字。"""
    from ai_web2api.providers.webchat import _pending_increment, _suffix_after

    assert _pending_increment("", "abc") == "abc"
    assert _pending_increment("abc", "abcdef") == "def"
    assert _pending_increment("abc", "abcd") == "d"
    assert _pending_increment("abc", "abc") == ""
    assert _pending_increment("abc", "ax") == ""          # 非前缀 → 不发脏数据
    assert _pending_increment("abcdef", "abc") == ""      # 变短 → 不发
    assert _suffix_after("", "全部") == "全部"
    assert _suffix_after("ab", "abcdef") == "cdef"
    assert _suffix_after("abc", "abdXYZ") == "dXYZ"       # 公共前缀 "ab" 之后全部补发


@pytest.mark.asyncio
async def test_done_toolbar_finalizes_early_without_stop_button(monkeypatch):
    """「消息工具栏出现」= 站点自己的完成信号 → 不必再等 stable_polls*3 拍。

    场景：站点**没有可用停止按钮**（如豆包），此前只能靠长稳定窗口（豆包实测 12.7s 白等）。
    """
    from ai_web2api.browser import extractor as ex

    prov = _prov(response_container=[".md"], thinking_container=[], stream_content=False,
                 done_toolbar=["[data-testid=copy]"])
    page = _FakePage()
    state = {"n": 0}
    text = "第一段。" * 3

    async def fake_count(_p, _sel):
        return 1

    async def fake_extract(_p, _sel, index=-1):
        state["n"] += 1
        return text if state["n"] > 1 else text[:3]

    async def fake_toolbar(_p, _resp, _tool, max_depth=5):
        # 第 5 拍起工具栏出现（正文此时已稳定 2 拍）
        return state["n"] >= 5

    monkeypatch.setattr(ex, "count_matches", fake_count)
    monkeypatch.setattr(ex, "extract_markdown", fake_extract)
    monkeypatch.setattr(ex, "has_done_toolbar", fake_toolbar)

    chunks = [(c.kind, c.text) async for c in prov._poll_response_dom(page, {".md": 0}, {})]
    assert "".join(t for k, t in chunks if k == "content") == text, "定稿内容必须完整"
    # 工具栏在第 5 拍出现 + 2 拍稳定 → 远早于稳定兜底（stable_polls*3 = 9 拍）
    assert state["n"] <= 9, f"应在工具栏出现后很快定稿，实际取了 {state['n']} 次文本"


@pytest.mark.asyncio
async def test_done_toolbar_absent_falls_back_to_stability(monkeypatch):
    """工具栏一直不出现（站点不显示/被隐藏）→ 仍走稳定性兜底，不能提前结束。"""
    from ai_web2api.browser import extractor as ex

    prov = _prov(response_container=[".md"], thinking_container=[], stream_content=False,
                 done_toolbar=["[data-testid=copy]"])
    page = _FakePage()
    state = {"n": 0}

    async def fake_count(_p, _sel):
        return 1

    async def fake_extract(_p, _sel, index=-1):
        state["n"] += 1
        return "完整正文"

    async def fake_toolbar(*_a, **_kw):
        return False

    monkeypatch.setattr(ex, "count_matches", fake_count)
    monkeypatch.setattr(ex, "extract_markdown", fake_extract)
    monkeypatch.setattr(ex, "has_done_toolbar", fake_toolbar)

    chunks = [(c.kind, c.text) async for c in prov._poll_response_dom(page, {".md": 0}, {})]
    assert "".join(t for k, t in chunks if k == "content") == "完整正文"
    assert state["n"] >= 3, "没有工具栏信号时必须继续轮询（不能一出现正文就结束）"
