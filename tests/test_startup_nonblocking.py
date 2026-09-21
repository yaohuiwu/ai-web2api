"""/ui 不能被启动登录检查阻塞。

uvicorn 在 lifespan yield 之前不对外服务；若把「登录状态检测 + 自动登录」await 在
yield 前，登录期间 `/ui` 会一直连不上。这里把两者放到后台任务，本测试验证
即使 refresh_login_status 很慢，进入 lifespan 后 `/ui` 仍立即可用。

运行：.venv/bin/python -m pytest tests/test_startup_nonblocking.py -v
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from ai_web2api.browser.manager import BrowserManager
from ai_web2api.providers.registry import ProviderRegistry

ROOT = Path(__file__).resolve().parent.parent


def _app(tmp_path: Path):
    from ai_web2api.main import create_app

    cfg = yaml.safe_load((ROOT / "config.fake.yaml").read_text())
    cfg["profiles_dir"] = str(tmp_path)
    cfg["providers"]["fake"]["url"] = f"file://{ROOT / 'tests' / 'fake_chat.html'}"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return create_app(str(cfg_path))


def test_ui_not_blocked_by_slow_login_check(tmp_path: Path, monkeypatch):
    async def _noop(self) -> None:  # 不真的启动浏览器
        return None

    async def _slow_refresh(self) -> None:  # 模拟很慢的登录检查
        await asyncio.sleep(5)

    monkeypatch.setattr(BrowserManager, "start", _noop)
    monkeypatch.setattr(BrowserManager, "stop", _noop)
    monkeypatch.setattr(ProviderRegistry, "refresh_login_status", _slow_refresh)

    app = _app(tmp_path)
    t0 = time.monotonic()
    with TestClient(app) as c:
        elapsed = time.monotonic() - t0
        assert c.get("/ui/").status_code == 200
        assert c.get("/healthz").status_code == 200
    # 若登录检查被 await 在 yield 前，这里会 ≥5s
    assert elapsed < 3, f"启动被登录检查阻塞了 {elapsed:.1f}s"
