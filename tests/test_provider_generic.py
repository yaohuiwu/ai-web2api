"""Provider 级通用配置：locale / login.url / session_url 模板。

运行：.venv/bin/python -m pytest tests/test_provider_generic.py -v
"""

from __future__ import annotations

from ai_web2api.config import ProviderConfig
from ai_web2api.providers.webchat import WebChatProvider


class _StubBrowser:
    default_locale = "zh-CN"


def _prov(**extra) -> WebChatProvider:
    cfg = ProviderConfig.model_validate(
        {
            "name": "qwen",
            "url": "https://chat.qwen.ai/",
            "models": [{"name": "qwen-max"}],
            **extra,
        }
    )
    return WebChatProvider(cfg, _StubBrowser())  # type: ignore[arg-type]


def test_locale_fallback_and_override():
    assert _prov().locale == "zh-CN"  # 未配 → 全局
    assert _prov(locale="en-US").locale == "en-US"  # provider 覆盖


def test_login_url_default_and_override():
    assert _prov().login_url == "https://chat.qwen.ai/"  # 默认=聊天页
    assert (
        _prov(login={"url": "https://chat.qwen.ai/auth"}).login_url
        == "https://chat.qwen.ai/auth"
    )


def test_session_url_template():
    assert _prov().session_url("abc") is None  # 未配模板
    p = _prov(session_url="{base}/c/{id}")
    assert p.session_url("abc") == "https://chat.qwen.ai/c/abc"


# ---- check_login 的“不确定”判定（页面还在加载屏时不应判未登录）----

import pytest  # noqa: E402


class _StuckPage:
    def __init__(self, splash_visible: bool = False, body: str = ""):
        self._splash_visible = splash_visible
        self._body = body

    def locator(self, sel):  # noqa: ANN001
        return self

    @property
    def first(self):
        return self

    async def is_visible(self) -> bool:
        return self._splash_visible

    async def inner_text(self, sel):  # noqa: ANN001
        return self._body


def _stuck_prov() -> WebChatProvider:
    return _prov(login={"page": {"splash": ["#splash-screen"]}})


@pytest.mark.asyncio
async def test_stuck_loading_true_on_visible_splash():
    assert await _stuck_prov()._page_stuck_loading(_StuckPage(splash_visible=True, body="hi")) is True


@pytest.mark.asyncio
async def test_stuck_loading_true_on_empty_body():
    assert await _stuck_prov()._page_stuck_loading(_StuckPage(splash_visible=False, body="  ")) is True


@pytest.mark.asyncio
async def test_stuck_loading_false_when_ready():
    assert await _stuck_prov()._page_stuck_loading(_StuckPage(splash_visible=False, body="新建对话")) is False


class _LoginStub(WebChatProvider):
    def __init__(self, cfg, fail_times: int):
        super().__init__(cfg, browser=None)  # type: ignore[arg-type]
        self.calls = 0
        self.fail_times = fail_times

    async def _auto_login_once(self) -> dict:
        self.calls += 1
        if self.calls <= self.fail_times:
            return {"ok": False, "reason": "transient"}
        return {"ok": True, "already_logged_in": False}


@pytest.mark.asyncio
async def test_auto_login_retries_until_success():
    cfg = ProviderConfig.model_validate(
        {"name": "qwen", "url": "https://x", "models": [{"name": "m"}],
         "login": {"mode": "auto", "retries": 3}}
    )
    p = _LoginStub(cfg, fail_times=2)
    p.LOGIN_RETRY_DELAY = 0
    r = await p.auto_login()
    assert r["ok"] is True and p.calls == 3 and r.get("attempts") == 3


@pytest.mark.asyncio
async def test_auto_login_gives_up_after_retries():
    cfg = ProviderConfig.model_validate(
        {"name": "qwen", "url": "https://x", "models": [{"name": "m"}],
         "login": {"mode": "auto", "retries": 3}}
    )
    p = _LoginStub(cfg, fail_times=99)
    p.LOGIN_RETRY_DELAY = 0
    r = await p.auto_login()
    assert r["ok"] is False and p.calls == 3


# ---- _send_prompt：type_prompt（contenteditable/React）vs fill ----

class _ElHandle:
    def __init__(self, page, sel):
        self._page = page
        self._sel = sel

    async def evaluate(self, script, *args):
        self._page.calls.append(("evaluate", self._sel, script, args))


class _Loc:
    def __init__(self, page, sel):
        self._page = page
        self._sel = sel

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self._sel in self._page.present else 0

    async def click(self):
        self._page.calls.append(("click", self._sel))

    async def fill(self, t):
        self._page.calls.append(("fill", t))

    async def press_sequentially(self, t, delay=0):
        self._page.calls.append(("type", t))

    async def element_handle(self):
        return _ElHandle(self._page, self._sel)


class _Page:
    def __init__(self, present=()):
        self.present = set(present)
        self.calls = []
        self.keys = []

    def locator(self, sel):
        return _Loc(self, sel)

    async def wait_for_timeout(self, ms):
        return None

    @property
    def keyboard(self):
        return self

    async def press(self, k):
        self.keys.append(k)


@pytest.mark.asyncio
async def test_send_prompt_type_mode():
    cfg = ProviderConfig.model_validate(
        {"name": "chatgpt", "url": "https://x", "models": [{"name": "m"}],
         "selectors": {"input": ["#prompt-textarea"], "type_prompt": True, "send_button": []}}
    )
    p = WebChatProvider(cfg, _StubBrowser())
    page = _Page(present={"#prompt-textarea"})
    await p._send_prompt(page, "#prompt-textarea", "hello")
    assert not any(c[0] == "type" for c in page.calls), "不应再逐字输入"
    assert any(c[0] == "evaluate" for c in page.calls), "应使用 JS 注入"
    assert ("fill", "hello") not in page.calls
    assert page.keys == ["Enter"]  # send_button 空 → 回车发送


@pytest.mark.asyncio
async def test_send_prompt_fill_mode_default():
    p = _prov()  # 默认 type_prompt=False
    page = _Page(present={"#prompt-textarea"})
    await p._send_prompt(page, "#prompt-textarea", "hi")
    assert ("fill", "hi") in page.calls and ("type", "hi") not in page.calls
