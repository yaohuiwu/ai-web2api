"""认证有效期计算 + state.json 读取。

运行：.venv/bin/python -m pytest tests/test_auth_expiry.py -v
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ai_web2api.core.auth_expiry import compute_auth_expiry, read_state_cookies

DAY = 86400.0
NOW = 1_800_000_000.0


def _c(name: str, expires: float) -> dict:
    return {"name": name, "value": "x", "expires": expires}


# ---------- 未配置 / 无命中 → unknown（绝不误报） ----------


def test_no_config_is_unknown():
    out = compute_auth_expiry([_c("aws-waf-token", NOW + 3 * DAY)], now=NOW)
    assert out.state == "unknown" and out.expires_at is None and out.source == "unknown"


def test_no_match_is_unknown():
    out = compute_auth_expiry(
        [_c("aws-waf-token", NOW + 3 * DAY)], auth_cookies=["token"], now=NOW
    )
    assert out.state == "unknown"


# ---------- 命中 cookie：取最早，状态判定 ----------


def test_picks_earliest_matching_cookie():
    out = compute_auth_expiry(
        [
            _c("__Secure-next-auth.session-token.0", NOW + 90 * DAY),
            _c("__Secure-next-auth.session-token.1", NOW + 89 * DAY),
            _c("aws-waf-token", NOW + 3 * DAY),  # 不匹配 → 忽略
        ],
        auth_cookies=["__Secure-next-auth.session-token*"],
        warn_days=7,
        now=NOW,
    )
    assert out.source == "cookie"
    assert out.cookie == "__Secure-next-auth.session-token.1"
    assert out.state == "ok" and round(out.days_left) == 89


def test_states_ok_soon_expired_boundaries():
    pats = ["token"]
    # 阈值边界：days_left == warn_days → soon
    assert compute_auth_expiry([_c("token", NOW + 3 * DAY)], auth_cookies=pats, warn_days=3, now=NOW).state == "soon"
    assert compute_auth_expiry([_c("token", NOW + 4 * DAY)], auth_cookies=pats, warn_days=3, now=NOW).state == "ok"
    # 已过期（含正好等于 now）
    assert compute_auth_expiry([_c("token", NOW - 1)], auth_cookies=pats, now=NOW).state == "expired"
    assert compute_auth_expiry([_c("token", NOW)], auth_cookies=pats, now=NOW).state == "expired"


def test_glob_and_case_insensitive():
    out = compute_auth_expiry(
        [_c("TOKEN", NOW + 5 * DAY)], auth_cookies=["token"], warn_days=1, now=NOW
    )
    assert out.state == "ok" and out.cookie == "TOKEN"


# ---------- 会话型 cookie：TTL 估算 ----------


def test_session_cookie_with_ttl_estimate():
    out = compute_auth_expiry(
        [_c("ds_session_id", -1)],
        auth_cookies=["ds_session_id"],
        session_ttl_days=10,
        state_mtime=NOW,
        warn_days=3,
        now=NOW,
    )
    assert out.source == "session_estimate"
    assert round(out.days_left) == 10 and out.state == "ok"


def test_session_cookie_without_ttl_is_unknown():
    out = compute_auth_expiry(
        [_c("ds_session_id", -1)], auth_cookies=["ds_session_id"], session_ttl_days=None, now=NOW
    )
    assert out.state == "unknown"


def test_expires_as_string_and_zero():
    out = compute_auth_expiry(
        [{"name": "token", "expires": "1800000000"}, _c("token2", 0)],
        auth_cookies=["token*"],
        now=NOW,
    )
    assert out.state == "expired" and out.expires_at == 1_800_000_000.0


# ---------- state.json 读取 ----------


def test_read_state_cookies_missing_and_cached(tmp_path: Path):
    p = tmp_path / "state.json"
    assert read_state_cookies(p) == []  # 不存在
    p.write_text(json.dumps({"cookies": [_c("token", time.time() + DAY)]}), encoding="utf-8")
    assert [c["name"] for c in read_state_cookies(p)] == ["token"]
    # 文件损坏 → 空表（不抛）
    p.write_text("{not json", encoding="utf-8")
    assert read_state_cookies(p) == []


def test_to_dict_shape():
    out = compute_auth_expiry(
        [_c("token", NOW + 6 * DAY)], auth_cookies=["token"], warn_days=7, now=NOW
    )
    d = out.to_dict()
    assert set(d) == {
        "state",
        "expires_at",
        "expires_at_iso",
        "days_left",
        "warn_days",
        "source",
        "cookie",
    }
    assert d["state"] == "soon" and d["expires_at_iso"].endswith("Z")


def test_compute_for_state_file(tmp_path: Path):
    from ai_web2api.core.auth_expiry import compute_for_state_file

    p = tmp_path / "state.json"
    p.write_text(
        json.dumps({"cookies": [_c("token", time.time() + 5 * DAY)]}), encoding="utf-8"
    )
    out = compute_for_state_file(p, auth_cookies=["token"], warn_days=1)
    assert out.state == "ok" and out.source == "cookie"
    # 文件不存在 → unknown（不抛）
    assert compute_for_state_file(tmp_path / "nope.json", auth_cookies=["token"]).state == "unknown"


def test_background_warns_only_manual_providers():
    """后台只在……前提：提醒逻辑仅对手动认证生效（静态断言）。"""
    src = (Path(__file__).resolve().parent.parent / "src" / "ai_web2api" / "main.py").read_text(
        encoding="utf-8"
    )
    assert "_warn_auth_expiry" in src
    assert 'p.cfg.login.mode != "manual"' in src
    assert "_auth_warned" in src  # 状态变化才记，避免刷屏
