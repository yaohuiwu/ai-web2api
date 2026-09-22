"""认证有效期配置字段：默认值 + 覆盖。

运行：.venv/bin/python -m pytest tests/test_auth_expiry_config.py -v
"""

from __future__ import annotations

from ai_web2api.config import BrowserConfig, ProviderConfig


def test_defaults():
    assert BrowserConfig().auth_expiry_warn_days == 3.0
    login = ProviderConfig.model_validate({"name": "x", "url": "https://x/", "models": [{"name": "m"}]}).login
    assert login.auth_cookies == []          # 不配 = 未知（不误报）
    assert login.session_ttl_days is None
    assert login.expiry_warn_days is None    # 未覆盖 → 用全局


def test_overrides():
    p = ProviderConfig.model_validate(
        {
            "name": "chatgpt",
            "url": "https://chatgpt.com/",
            "models": [{"name": "gpt-5-web"}],
            "login": {
                "mode": "manual",
                "auth_cookies": ["__Secure-next-auth.session-token*"],
                "expiry_warn_days": 7,
                "session_ttl_days": 10,
            },
        }
    )
    assert p.login.auth_cookies == ["__Secure-next-auth.session-token*"]
    assert p.login.expiry_warn_days == 7
    assert p.login.session_ttl_days == 10
