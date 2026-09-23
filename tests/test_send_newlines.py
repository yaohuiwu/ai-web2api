"""发送提示词：多行内容必须用 Shift+Enter 换行，不能用 Enter（会被当成“发送”截断）。

运行：.venv/bin/python -m pytest tests/test_send_newlines.py -v
"""

from __future__ import annotations

import pytest

from ai_web2api.config import ProviderConfig
from ai_web2api.providers.webchat import WebChatProvider


class _StubBrowser:
    default_locale = "zh-CN"


class _ElHandle:
    def __init__(self, rec, content_editable=True):
        self._rec = rec
        self._content_editable = content_editable

    async def evaluate(self, script, *args):
        self._rec.append(("evaluate", script, args))
        if "isContentEditable" in script:
            return self._content_editable


class _Input:
    def __init__(self, rec: list, content_editable: bool = True) -> None:
        self.rec = rec
        self._content_editable = content_editable

    async def element_handle(self):
        return _ElHandle(self.rec, self._content_editable)


class _Keyboard:
    def __init__(self, rec: list) -> None:
        self.rec = rec

    async def press(self, key: str) -> None:
        self.rec.append(("key", key))


class _Page:
    def __init__(self) -> None:
        self.rec: list = []
        self.keyboard = _Keyboard(self.rec)


def _prov(**selectors) -> WebChatProvider:
    cfg = ProviderConfig.model_validate(
        {
            "name": "chatgpt",
            "url": "https://chatgpt.com/",
            "models": [{"name": "gpt-5-web"}],
            "selectors": selectors,
        }
    )
    return WebChatProvider(cfg, _StubBrowser())  # type: ignore[arg-type]



@pytest.mark.asyncio
async def test_newlines_use_shift_enter():
    prov, page = _prov(), _Page()
    inp = _Input(page.rec)
    await prov._type_prompt_human(page, inp, "line1\nline2\n\nline4")

    # JS 注入：应调用 evaluate 执行 JS 脚本（含 <br> 换行）
    evals = [item for item in page.rec if item[0] == "evaluate"]
    assert len(evals) >= 1, "应有 JS 注入调用"
    # 检查脚本中用 createElement('br') 处理换行
    scripts = [e[1] for e in evals if isinstance(e[1], str)]
    assert any("createElement('br')" in s for s in scripts), "contenteditable 换行应用 <br>"
    # 不应再有为换行按 Shift+Enter
    keys = [item[1] for item in page.rec if item[0] == "key"]
    assert "Shift+Enter" not in keys
    assert "Enter" not in keys


@pytest.mark.asyncio
async def test_crlf_normalized():
    prov, page = _prov(), _Page()
    await prov._type_prompt_human(page, _Input(page.rec), "a\r\nb\rc")
    # JS 注入应接收规范化后的文本（\r\n 和 \r 都转为 \n）
    evals = [item for item in page.rec if item[0] == "evaluate"]
    assert len(evals) >= 1
    # 检查传入的文本中不含 \r
    for kind, script, args in evals:
        if kind == "evaluate" and isinstance(args, tuple) and args:
            assert "\r" not in str(args[0]), f"文本应已规范化，收到: {args[0]!r}"


@pytest.mark.asyncio
async def test_single_line_has_no_newline_key():
    prov, page = _prov(), _Page()
    await prov._type_prompt_human(page, _Input(page.rec), "可用工具:[{...}]")
    # 单行无换行，JS 注入中不应有 <br>
    # 单行无换行，JS 注入的文本参数不应含 \n
    evals = [item for item in page.rec if item[0] == "evaluate"]
    assert len(evals) >= 1
    # 找到 contenteditable 注入调用的文本参数
    for kind, script, args in evals:
        if kind == "evaluate" and isinstance(args, tuple) and args:
            assert "\n" not in args[0], f"单行文本不应含换行: {args[0]!r}"


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
    async def _visible(_page, _sel):
        return True

    monkeypatch.setattr(WebChatProvider, "_is_visible", staticmethod(_visible))
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


# ---------- 弹窗自动关闭 / 繁忙提示（Kimi 排队弹窗实测） ----------


class _Btn:
    def __init__(self, visible=True, fail=False):
        self._visible = visible
        self._fail = fail
        self.clicked = 0

    @property
    def first(self):
        return self

    async def is_visible(self):
        return self._visible

    async def click(self, timeout=0):
        if self._fail:
            raise RuntimeError("click intercepted")
        self.clicked += 1


class _Locator:
    """模拟 Playwright locator：count()/nth()/is_visible()/click()。"""

    def __init__(self, items, fail=False):
        self._items = list(items)
        self._fail = fail
        self.clicked = 0

    async def count(self):
        return len(self._items)

    def nth(self, i):
        return self._items[i]

    async def is_visible(self):
        return False

    async def click(self, timeout=0):
        raise RuntimeError("noop")


class _OverlayPage:
    def __init__(self, mapping):
        self._mapping = mapping

    def locator(self, sel):
        return self._mapping[sel]

    async def wait_for_timeout(self, _ms):
        return None


@pytest.mark.asyncio
async def test_dismiss_overlays_clicks_visible_only():
    prov = _prov(dismiss_button=["button.a", "button.b"])
    hidden, visible = _Btn(visible=False), _Btn(visible=True)
    out = await prov._dismiss_overlays(
        _OverlayPage({"button.a": _Locator([hidden, visible]), "button.b": _Locator([_Btn(visible=False)])})
    )
    assert out == ["button.a"], "应点到第 2 个（可见的那个），而不是 .first（隐藏模板）"
    assert visible.clicked == 1 and hidden.clicked == 0


@pytest.mark.asyncio
async def test_dismiss_overlays_swallows_click_errors():
    """关不掉也不能影响主流程。"""
    prov = _prov(dismiss_button=["button.a"])
    out = await prov._dismiss_overlays(
        _OverlayPage({"button.a": _Locator([_Btn(visible=True, fail=True)])})
    )
    assert out == []


@pytest.mark.asyncio
async def test_busy_hint_visible(monkeypatch):
    from ai_web2api.providers.webchat import WebChatProvider

    prov = _prov(busy_hint=[".modal:has-text('优先队列')"])

    async def _vis(_page, sel):
        return sel == ".modal:has-text('优先队列')"

    monkeypatch.setattr(WebChatProvider, "_is_visible", staticmethod(_vis))
    assert await prov._busy_hint_visible(_OverlayPage({})) is True

    prov2 = _prov()
    assert await prov2._busy_hint_visible(_OverlayPage({})) is False
