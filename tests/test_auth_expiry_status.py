"""/admin/status 暴露 auth_expiry（认证有效期）。

运行：.venv/bin/python -m pytest tests/test_auth_expiry_status.py -v
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def _app(tmp_path: Path, login_extra: dict | None = None):
    from ai_web2api.main import create_app

    cfg = yaml.safe_load((ROOT / "config.fake.yaml").read_text())
    cfg["profiles_dir"] = str(tmp_path)
    cfg["providers"]["fake"]["url"] = f"file://{ROOT / 'tests' / 'fake_chat.html'}"
    if login_extra:
        cfg["providers"]["fake"].setdefault("login", {}).update(login_extra)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return create_app(str(cfg_path))


def _write_state(tmp_path: Path, cookies: list[dict]) -> None:
    p = tmp_path / "fake"
    p.mkdir(parents=True, exist_ok=True)
    (p / "state.json").write_text(json.dumps({"cookies": cookies}), encoding="utf-8")


def _client_logged_in(app) -> TestClient:
    """强制 fake provider 为已登录（否则 /admin/status 会把 state 覆盖成 logged_out）。"""
    app.state.registry.login_status = lambda: {"fake": True}
    return TestClient(app)


def _prov(c: TestClient) -> dict:
    return {x["name"]: x for x in c.get("/admin/status").json()["providers"]}["fake"]


def test_status_exposes_auth_expiry_soon(tmp_path: Path):
    c = _client_logged_in(_app(tmp_path, {"auth_cookies": ["token"], "expiry_warn_days": 7}))
    _write_state(tmp_path, [{"name": "token", "value": "x", "expires": time.time() + 2 * 86400}])
    ae = _prov(c)["auth_expiry"]
    assert ae["state"] == "soon"
    assert ae["source"] == "cookie" and ae["cookie"] == "token"
    assert ae["warn_days"] == 7
    assert 1.9 < ae["days_left"] < 2.1
    assert ae["expires_at_iso"].endswith("Z")


def test_status_unknown_without_auth_cookies(tmp_path: Path):
    """未配置 auth_cookies → unknown（即便有 WAF 等无关 cookie 也不误报）。"""
    c = _client_logged_in(_app(tmp_path))
    _write_state(tmp_path, [{"name": "aws-waf-token", "value": "x", "expires": time.time() + 3 * 86400}])
    ae = _prov(c)["auth_expiry"]
    assert ae["state"] == "unknown" and ae["expires_at"] is None and ae["source"] == "unknown"


def test_webui_renders_auth_expiry():
    js = (ROOT / "src" / "ai_web2api" / "webui" / "assets" / "js" / "index.js").read_text(
        encoding="utf-8"
    )
    assert "认证有效期" in js
    assert "p.auth_expiry" in js
    assert 'p.login_mode === "manual"' in js  # 仅手动认证提醒
    assert "expired" in js and "soon" in js
