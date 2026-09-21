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
