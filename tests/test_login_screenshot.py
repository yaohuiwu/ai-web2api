"""登录失败截图：BrowserManager 保存 + admin 路由 + 状态面板展示。

运行：.venv/bin/python -m pytest tests/test_login_screenshot.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ai_web2api.browser.manager import BrowserManager
from ai_web2api.config import BrowserConfig

ROOT = Path(__file__).resolve().parent.parent
WEBUI_JS = ROOT / "src" / "ai_web2api" / "webui" / "assets" / "js" / "index.js"


class _StubPage:
    async def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_save_and_clear_login_error(tmp_path: Path):
    bm = BrowserManager(BrowserConfig(), tmp_path)
    assert bm.login_error_path("qwen") == tmp_path / "qwen" / "login_error.png"
    assert bm.login_error_mtime("qwen") is None

    assert await bm.save_login_error("qwen", _StubPage()) is not None
    assert bm.login_error_path("qwen").exists()
    assert bm.login_error_mtime("qwen") is not None

    bm.clear_login_error("qwen")
    assert not bm.login_error_path("qwen").exists()


def _app(tmp_path: Path):
    from ai_web2api.main import create_app

    cfg = yaml.safe_load((ROOT / "config.fake.yaml").read_text())
    cfg["profiles_dir"] = str(tmp_path)
    cfg["providers"]["fake"]["url"] = f"file://{ROOT / 'tests' / 'fake_chat.html'}"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return create_app(str(cfg_path))


def test_screenshot_endpoint_404_then_200(tmp_path: Path):
    c = TestClient(_app(tmp_path))
    assert c.get("/admin/fake/login/screenshot").status_code == 404

    p = tmp_path / "fake"
    p.mkdir(parents=True, exist_ok=True)
    (p / "login_error.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    r = c.get("/admin/fake/login/screenshot")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


def test_status_exposes_screenshot_flag(tmp_path: Path):
    c = TestClient(_app(tmp_path))
    p = tmp_path / "fake"
    p.mkdir(parents=True, exist_ok=True)
    (p / "login_error.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    prov = {x["name"]: x for x in c.get("/admin/status").json()["providers"]}["fake"]
    assert prov["login_error_screenshot"] is True
    assert prov["login_error_at"] is not None


def test_webui_references_screenshot():
    js = WEBUI_JS.read_text(encoding="utf-8")
    assert "login/screenshot" in js
    assert "actScreenshot" in js
    assert "login_error_screenshot" in js
    # 未登录引导：导入面板 + 复制命令
    assert "initManualPanel" in js
    assert "login-cta" in js
    assert "copyText" in js
