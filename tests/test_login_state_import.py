"""POST /admin/{p}/login/state：导入 storage_state（写盘 + reset_context）。

运行：.venv/bin/python -m pytest tests/test_login_state_import.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_web2api.api.routes import create_router

STATE = {
    "cookies": [
        {"name": "sid", "value": "abc", "domain": ".qwen.ai", "path": "/", "expires": 9999999999}
    ],
    "origins": [{"origin": "https://chat.qwen.ai", "localStorage": [{"name": "t", "value": "1"}]}],
}


class _Browser:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.reset: list[str] = []
        self.logins: list[str] = []

    def state_path(self, name: str) -> Path:
        return self.root / name / "state.json"

    async def reset_context(self, name: str) -> None:
        self.reset.append(name)

    def clear_login_error(self, name: str) -> None:
        pass

    def record_login_at(self, name: str, ts: float | None = None) -> float:
        self.logins.append(name)
        return 0.0


class _Provider:
    name = "fake"

    def __init__(self, browser: _Browser) -> None:
        self.browser = browser
        self.cfg = SimpleNamespace()

    async def check_login(self) -> bool:
        return True


class _Registry:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider
        self._status: dict[str, bool] = {}

    def get_provider(self, name: str) -> _Provider:
        return self.provider

    def login_status(self) -> dict[str, bool]:
        return dict(self._status)

    def set_login_status(self, name: str, ok: bool) -> None:
        self._status[name] = ok


def _client(tmp_path: Path):
    browser = _Browser(tmp_path)
    reg = _Registry(_Provider(browser))
    app = FastAPI()
    app.include_router(create_router(reg, None))  # type: ignore[arg-type]
    return TestClient(app), browser, tmp_path


def test_import_writes_state_and_resets_context(tmp_path: Path):
    c, browser, root = _client(tmp_path)
    r = c.post("/admin/fake/login/state", json=STATE)
    assert r.status_code == 200
    body = r.json()
    assert body["imported"] is True and body["logged_in"] is True
    # 写盘
    written = json.loads((root / "fake" / "state.json").read_text(encoding="utf-8"))
    assert written["cookies"][0]["name"] == "sid"
    assert written["origins"][0]["origin"] == "https://chat.qwen.ai"
    # 重置 context
    assert browser.reset == ["fake"]
    # 导入 = 一次新登录 → 记录首次登录时间
    assert browser.logins == ["fake"]


def test_import_rejects_empty_cookies(tmp_path: Path):
    c, _, _ = _client(tmp_path)
    r = c.post("/admin/fake/login/state", json={"cookies": [], "origins": []})
    assert r.status_code == 400
