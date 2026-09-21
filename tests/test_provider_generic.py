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
