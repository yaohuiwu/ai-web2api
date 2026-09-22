"""认证有效期：从 state.json 的 cookie 推导「登录态何时过期」。

原则：**只认配置声明的 ``auth_cookies``（glob）**，绝不回退到「最早到期的 cookie」——
那会命中 WAF/偏好 cookie（如 deepseek 的 ``aws-waf-token`` 3 天、chatgpt 的 Google
``SID`` 399 天）→ 误报。无法判断时返回 ``unknown``（UI 显示「未知」）。
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import time
from dataclasses import dataclass
from pathlib import Path

_UNKNOWN = "unknown"


@dataclass(frozen=True)
class AuthExpiry:
    """认证有效期结果（``state``: ok | soon | expired | unknown）。"""

    state: str
    expires_at: float | None
    days_left: float | None
    warn_days: float
    source: str  # cookie | session_estimate | unknown
    cookie: str | None = None
    saved_at: float | None = None  # state.json 最后更新时间（epoch 秒）
    login_at: float | None = None  # 首次/本次登录时间（sidecar，不随轮换变化）
    login_at_source: str | None = None  # recorded | state_file（无 sidecar 时回退 state.json mtime）

    def to_dict(self) -> dict:
        def iso(ts: float | None) -> str | None:
            if ts is None:
                return None
            return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )

        # 首次登录时间：优先 sidecar 记录；无则回退 state.json mtime（标注来源）
        login_at, login_src = self.login_at, self.login_at_source
        if login_at is None and self.saved_at is not None:
            login_at, login_src = self.saved_at, "state_file"
        # 推算有效期 = 到期时间 − 首次登录时间，仅当两者都有且为正
        validity_days = None
        if self.expires_at is not None and login_at is not None:
            span = (self.expires_at - login_at) / 86400.0
            if span > 0:
                validity_days = round(span, 2)
        return {
            "state": self.state,
            "expires_at": self.expires_at,
            "expires_at_iso": iso(self.expires_at),
            "days_left": self.days_left,
            "warn_days": self.warn_days,
            "source": self.source,
            "cookie": self.cookie,
            "saved_at": self.saved_at,
            "saved_at_iso": iso(self.saved_at),
            "login_at": login_at,
            "login_at_iso": iso(login_at),
            "login_at_source": login_src,
            "validity_days": validity_days,
        }


def _cookie_expiry(cookie: dict) -> float | None:
    """cookie 的正数 ``expires``（会话型 -1 / 缺失 → None）。"""
    raw = cookie.get("expires")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def compute_auth_expiry(
    cookies: list[dict],
    *,
    auth_cookies: list[str] | None = None,
    session_ttl_days: float | None = None,
    state_mtime: float | None = None,
    warn_days: float = 3.0,
    login_at: float | None = None,
    now: float | None = None,
) -> AuthExpiry:
    """按配置的 ``auth_cookies`` 计算认证有效期。

    - 命中 cookie 里有正数 ``expires`` → 取最早者（``source=cookie``）；
    - 命中的全是会话型（``-1``）且有 ``session_ttl_days`` → ``state_mtime + ttl``（``session_estimate``）；
    - 其余（未配置 / 无命中 / 会话型且无 TTL）→ ``unknown``。
    """
    now = time.time() if now is None else now
    patterns = [p.lower() for p in (auth_cookies or []) if p]
    matched: list[dict] = []
    if patterns:
        for cookie in cookies or []:
            name = str(cookie.get("name") or "")
            if name and any(fnmatch.fnmatch(name.lower(), pat) for pat in patterns):
                matched.append(cookie)

    dated = [(c, exp) for c in matched if (exp := _cookie_expiry(c)) is not None]
    if dated:
        cookie, expires_at = min(dated, key=lambda pair: pair[1])
        source = "cookie"
        cookie_name = str(cookie.get("name") or "")
    elif matched and session_ttl_days and state_mtime:
        expires_at = float(state_mtime) + float(session_ttl_days) * 86400.0
        source = "session_estimate"
        cookie_name = str(matched[0].get("name") or "")
    else:
        return AuthExpiry(
            _UNKNOWN, None, None, warn_days, _UNKNOWN, None,
            saved_at=state_mtime,
            login_at=login_at,
            login_at_source="recorded" if login_at else None,
        )

    days_left = (expires_at - now) / 86400.0
    if expires_at <= now:
        state = "expired"
    elif days_left <= warn_days:
        state = "soon"
    else:
        state = "ok"
    return AuthExpiry(
        state, expires_at, round(days_left, 2), warn_days, source, cookie_name,
        saved_at=state_mtime,
        login_at=login_at,
        login_at_source="recorded" if login_at else None,
    )


def compute_for_state_file(
    path: Path,
    *,
    auth_cookies: list[str] | None = None,
    session_ttl_days: float | None = None,
    warn_days: float = 3.0,
    login_at: float | None = None,
    now: float | None = None,
) -> AuthExpiry:
    """便捷封装：直接从 provider 的 state.json 路径计算（供路由与后台复用）。"""
    try:
        mtime: float | None = path.stat().st_mtime
    except OSError:
        mtime = None
    return compute_auth_expiry(
        read_state_cookies(path),
        auth_cookies=auth_cookies,
        session_ttl_days=session_ttl_days,
        state_mtime=mtime,
        warn_days=warn_days,
        login_at=login_at,
        now=now,
    )


# ---------- state.json 读取（按 mtime 缓存，避免状态轮询反复读盘） ----------

_CACHE: dict[str, tuple[float, list[dict]]] = {}


def read_state_cookies(path: Path) -> list[dict]:
    """读 state.json 的 cookies；文件不存在/损坏返回空表。按 mtime 缓存。"""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _CACHE.pop(key, None)
        return []
    cached = _CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cookies = list(data.get("cookies") or [])
    except Exception:  # noqa: BLE001
        cookies = []
    if len(_CACHE) > 64:
        _CACHE.clear()
    _CACHE[key] = (mtime, cookies)
    return cookies
