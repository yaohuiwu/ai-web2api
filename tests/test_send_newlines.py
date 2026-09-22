"""发送提示词：多行内容必须用 Shift+Enter 换行，不能用 Enter（会被当成“发送”截断）。

运行：.venv/bin/python -m pytest tests/test_send_newlines.py -v
"""

from __future__ import annotations

import pytest

from ai_web2api.config import ProviderConfig
from ai_web2api.providers.webchat import WebChatProvider


class _StubBrowser:
    default_locale = "zh-CN"


class _Input:
    def __init__(self, rec: list) -> None:
        self.rec = rec

    async def press_sequentially(self, text: str, delay: float = 0) -> None:
        self.rec.append(("type", text))


class _Keyboard:
    def __init__(self, rec: list) -> None:
        self.rec = rec

    async def press(self, key: str) -> None:
        self.rec.append(("key", key))


class _Page:
    def __init__(self) -> None:
        self.rec: list = []
        self.keyboard = _Keyboard(self.rec)


def _prov() -> WebChatProvider:
    cfg = ProviderConfig.model_validate(
        {"name": "chatgpt", "url": "https://chatgpt.com/", "models": [{"name": "gpt-5-web"}]}
    )
    return WebChatProvider(cfg, _StubBrowser())  # type: ignore[arg-type]



@pytest.mark.asyncio
async def test_newlines_use_shift_enter():
    prov, page = _prov(), _Page()
    inp = _Input(page.rec)
    await prov._type_prompt_human(page, inp, "line1\nline2\n\nline4")

    typed = [t for kind, t in page.rec if kind == "type"]
    keys = [k for kind, k in page.rec if kind == "key"]
    assert typed == ["line1", "line2", "line4"]  # 空行不输入字符
    assert keys == ["Shift+Enter", "Shift+Enter", "Shift+Enter"]  # 3 个换行
    assert "Enter" not in keys  # 绝不裸 Enter（会提前发送）


@pytest.mark.asyncio
async def test_crlf_normalized():
    prov, page = _prov(), _Page()
    await prov._type_prompt_human(page, _Input(page.rec), "a\r\nb\rc")
    assert [t for kind, t in page.rec if kind == "type"] == ["a", "b", "c"]
    assert [k for kind, k in page.rec if kind == "key"] == ["Shift+Enter", "Shift+Enter"]


@pytest.mark.asyncio
async def test_single_line_has_no_newline_key():
    prov, page = _prov(), _Page()
    await prov._type_prompt_human(page, _Input(page.rec), "可用工具:[{...}]")
    assert [k for kind, k in page.rec if kind == "key"] == []


# ---------- 发送确认：点发送 ≠ 消息发出（实测 ChatGPT 会静默丢掉） ----------


class _Loc:
    def __init__(self, values=None, text="", broken=False):
        self._values = list(values or [])
        self._text = text
        self._broken = broken

    @property
    def first(self):
        return self

    async def input_value(self):
        if self._broken or not self._values:
            raise RuntimeError("not an input element")
        return self._values.pop(0) if len(self._values) > 1 else self._values[0]

    async def inner_text(self):
        if self._broken:
            raise RuntimeError("element gone")
        return self._text


class _ConfirmPage:
    def __init__(self, loc):
        self._loc = loc

    def locator(self, _sel):
        return self._loc

    async def wait_for_timeout(self, _ms):
        return None


@pytest.mark.asyncio
async def test_confirm_sent_textarea_cleared():
    """textarea：输入框被清空 = 已发出。"""
    prov = _prov()
    page = _ConfirmPage(_Loc(values=["娱乐新闻", ""]))
    assert await prov._confirm_sent(page, "#ta", "娱乐新闻") is True


@pytest.mark.asyncio
async def test_confirm_sent_still_holds_prompt_is_false():
    """contenteditable 仍留着原文 → 判定未发出（触发重试）。"""
    prov = _prov()
    page = _ConfirmPage(_Loc(values=[], text="娱乐新闻"))
    assert await prov._confirm_sent(page, ".editor", "娱乐新闻", timeout=0.2) is False


@pytest.mark.asyncio
async def test_confirm_sent_missing_element_is_ok():
    """元素读不到 → 视为已发送（不误伤正常流程）。"""
    prov = _prov()
    assert await prov._confirm_sent(_ConfirmPage(_Loc(broken=True)), ".editor", "x") is True


@pytest.mark.asyncio
async def test_confirm_sent_text_changed_counts_as_sent():
    """输入框内容变了（例如被清成别的）也算已发出。"""
    prov = _prov()
    page = _ConfirmPage(_Loc(values=["", "别的文本"]))
    assert await prov._confirm_sent(page, "#ta", "娱乐新闻") is True


# ---------- 送达判定：区分"被静默丢弃"与"只是生成慢" ----------


class _DeliverPage:
    def __init__(self, counts):
        self._counts = counts


@pytest.mark.asyncio
async def test_message_delivered_via_new_container(monkeypatch):
    from ai_web2api.browser import extractor

    prov = _prov()

    async def fake_count(_page, _sel):
        return 4          # 发送前是 3 → 增加了

    monkeypatch.setattr(extractor, "count_matches", fake_count)
    assert await prov._message_delivered(_DeliverPage({}), {".md": 3}, {}) is True


@pytest.mark.asyncio
async def test_message_delivered_via_stop_button(monkeypatch):
    """容器还没出，但停止按钮可见 = 正在生成（只是慢）。"""
    from ai_web2api.browser import extractor
    from ai_web2api.providers.webchat import WebChatProvider

    prov = _prov(stop_button=["button.stop"])

    async def fake_count(_page, _sel):
        return 3          # 没增加

    monkeypatch.setattr(extractor, "count_matches", fake_count)
    monkeypatch.setattr(WebChatProvider, "_is_visible", staticmethod(lambda page, sel: True))
    assert await prov._message_delivered(_DeliverPage({}), {".md": 3}, {}) is True


@pytest.mark.asyncio
async def test_message_not_delivered(monkeypatch):
    """既无新容器也无停止按钮 → 判定消息没真正发出（触发重发/快速失败）。"""
    from ai_web2api.browser import extractor

    prov = _prov(stop_button=["button.stop"])

    async def fake_count(_page, _sel):
        return 3

    monkeypatch.setattr(extractor, "count_matches", fake_count)
    assert await prov._message_delivered(_DeliverPage({}), {".md": 3}, {}) is False
