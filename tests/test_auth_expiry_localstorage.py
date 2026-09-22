"""localStorage JWT 参与认证有效期（Kimi/DeepSeek 的 token 不在 cookie 里）。

运行：.venv/bin/python -m pytest tests/test_auth_expiry_localstorage.py -v
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from ai_web2api.core.auth_expiry import (
    compute_auth_expiry,
    compute_for_state_file,
    jwt_exp,
    read_state_local_storage,
)

DAY = 86400.0
NOW = 1_800_000_000.0
LS = "https://www.kimi.com"


def _jwt(exp: float | None, sub: str = "x") -> str:
    def seg(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    payload = {"sub": sub} if exp is None else {"sub": sub, "exp": int(exp)}
    return f"{seg({'alg': 'HS512', 'typ': 'JWT'})}.{seg(payload)}.sig"


def test_jwt_exp_parsing():
    assert jwt_exp(_jwt(NOW + 10 * DAY)) == float(int(NOW + 10 * DAY))
    assert jwt_exp("not-a-jwt") is None
    assert jwt_exp("") is None
    assert jwt_exp("a.b") is None
    assert jwt_exp(_jwt(None)) is None           # 无 exp
    assert jwt_exp("x.!!!invalid!!!.y") is None   # 坏 base64


def test_local_storage_token_drives_expiry():
    out = compute_auth_expiry(
        [_jwt_cookie := {"name": "sid", "value": "v", "expires": -1}],  # 只有会话 cookie
        auth_local_storage=["refresh_token"],
        local_storage=[(LS, "refresh_token", _jwt(NOW + 90 * DAY))],
        warn_days=7,
        now=NOW,
    )
    assert out.state == "ok" and out.source == "local_storage"
    assert out.cookie == "refresh_token" and round(out.days_left) == 90


def test_earliest_of_cookie_and_local_storage_wins():
    out = compute_auth_expiry(
        [{"name": "token", "value": "v", "expires": NOW + 3 * DAY}],
        auth_cookies=["token"],
        auth_local_storage=["refresh_token"],
        local_storage=[(LS, "refresh_token", _jwt(NOW + 90 * DAY))],
        warn_days=7,
        now=NOW,
    )
    assert out.source == "cookie" and out.cookie == "token" and out.state == "soon"


def test_local_storage_glob_and_missing():
    # glob 匹配（access_token 几分钟 → 会显示即将过期，正是我们想暴露的）
    out = compute_auth_expiry(
        [],
        auth_local_storage=["*_token"],
        local_storage=[(LS, "access_token", _jwt(NOW + 120)), (LS, "refresh_token", _jwt(NOW + 90 * DAY))],
        warn_days=1,
        now=NOW,
    )
    assert out.source == "local_storage" and out.cookie == "access_token"
    # 没有配置 / 没有匹配 → unknown
    assert compute_auth_expiry([], auth_local_storage=["nope"], local_storage=[], now=NOW).state == "unknown"


def test_read_state_local_storage_and_compute_for_state_file(tmp_path: Path):
    p = tmp_path / "state.json"
    p.write_text(
        json.dumps(
            {
                "cookies": [{"name": "sid", "value": "v", "expires": -1}],
                "origins": [{"origin": LS, "localStorage": [{"name": "refresh_token", "value": _jwt(time.time() + 30 * DAY)}]}],
            }
        ),
        encoding="utf-8",
    )
    assert read_state_local_storage(p)[0][1] == "refresh_token"
    out = compute_for_state_file(p, auth_local_storage=["refresh_token"], warn_days=7)
    assert out.state == "ok" and out.source == "local_storage" and 29 <= out.days_left <= 30


def test_login_config_has_auth_local_storage():
    from ai_web2api.config import ProviderConfig

    assert ProviderConfig.model_validate(
        {"name": "kimi", "url": "https://www.kimi.com/", "models": [{"name": "kimi-web"}]}
    ).login.auth_local_storage == []
    p = ProviderConfig.model_validate(
        {
            "name": "kimi",
            "url": "https://www.kimi.com/",
            "models": [{"name": "kimi-web"}],
            "login": {"auth_local_storage": ["refresh_token"]},
        }
    )
    assert p.login.auth_local_storage == ["refresh_token"]
