"""storage_state 落盘策略：已登录且未过期就不写；关闭浏览器时不能把 shutdown 打崩。

两件事：
1. 后台循环原先每 300s 无条件重写 state.json（即使登录态早着呢）——现在只有
   登录态变化 / 首次落盘 / cookie 快过期时才写。
2. Ctrl+C 会把整个进程组（含 Playwright 的 node 驱动）一起打掉，此时
   ``browser.close()`` 必然抛 RuntimeError → uvicorn 报 "Application shutdown failed"。
   stop() 必须尽力而为、绝不向上抛。

运行：.venv/bin/python -m pytest tests/test_state_save_policy.py -v
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from ai_web2api.browser.manager import BrowserManager
from ai_web2api.config import BrowserConfig

pytestmark = pytest.mark.asyncio  # pytest-asyncio strict 模式

PROVIDER = "deepseek"


class StubContext:
    """只实现 storage_state 的 context：把当前 cookie 写到 path。"""

    def __init__(self, cookies: list[dict], *, fail: bool = False):
        self.cookies = cookies
        self.fail = fail
        self.writes = 0

    async def storage_state(self, path: str, **_kw) -> None:
        self.writes += 1
        if self.fail:
            raise RuntimeError("driver gone")
        Path(path).write_text(json.dumps({"cookies": self.cookies, "origins": []}))


class StubBrowser:
    def __init__(self, *, close_raises: bool = False):
        self.close_calls = 0
        self.close_raises = close_raises
        self.closed = False

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_raises:
            raise RuntimeError(
                "Browser.close: unable to perform operation on <WriteUnixTransport closed=True>; "
                "the handler is closed"
            )
        self.closed = True


class StubPlaywright:
    def __init__(self, *, stop_raises: bool = False):
        self.stop_calls = 0
        self.stop_raises = stop_raises

    async def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_raises:
            raise RuntimeError("transport closed")


def _manager(tmp_path: Path, ctx: StubContext | None = None, **cfg) -> BrowserManager:
    mgr = BrowserManager(BrowserConfig(**cfg), tmp_path)
    if ctx is not None:
        mgr._contexts[PROVIDER] = ctx  # type: ignore[assignment]
    return mgr


def _write_state(tmp_path: Path, expires: float | None) -> None:
    path = tmp_path / PROVIDER / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    cookies = []
    if expires is not None:
        cookies.append({"name": "token", "expires": expires})
    path.write_text(json.dumps({"cookies": cookies, "origins": []}))


# ---------- 该不该写 ----------

async def test_missing_file_needs_save(tmp_path: Path) -> None:
    assert _manager(tmp_path).state_needs_save(PROVIDER) is True


async def test_logged_in_and_not_expiring_skips_save(tmp_path: Path, caplog) -> None:
    """核心诉求：已登录 + 登录态未过期 → 不 save。"""
    _write_state(tmp_path, time.time() + 30 * 86400)
    ctx = StubContext([])
    mgr = _manager(tmp_path, ctx)
    assert mgr.state_needs_save(PROVIDER) is False
    with caplog.at_level(logging.DEBUG):
        assert await mgr.save_state(PROVIDER) is False
    assert ctx.writes == 0, "不该真的写盘"
    assert (tmp_path / PROVIDER / "state.json").exists()


async def test_expiring_soon_triggers_save(tmp_path: Path) -> None:
    _write_state(tmp_path, time.time() + 3600)  # 1 小时后过期 < 默认 24h margin
    ctx = StubContext([{"name": "token", "expires": time.time() + 3600}])
    mgr = _manager(tmp_path, ctx)
    assert mgr.state_needs_save(PROVIDER) is True
    assert await mgr.save_state(PROVIDER) is True
    assert ctx.writes == 1


async def test_session_cookies_are_not_considered_expiring(tmp_path: Path) -> None:
    """全是会话 cookie（expires=-1）→ 没有"过期"概念，登录态没变就不写。"""
    _write_state(tmp_path, None)
    assert _manager(tmp_path).state_expiry(PROVIDER) is None
    assert _manager(tmp_path).state_needs_save(PROVIDER) is False


async def test_dirty_state_forces_one_save(tmp_path: Path) -> None:
    """登录态刚变化（login/cookies、自动登录）→ 即使没过期也必须写，写一次后清标记。"""
    _write_state(tmp_path, time.time() + 30 * 86400)
    ctx = StubContext([{"name": "token", "expires": time.time() + 30 * 86400}])
    mgr = _manager(tmp_path, ctx)
    mgr.mark_state_dirty(PROVIDER)
    assert await mgr.save_state(PROVIDER) is True
    assert ctx.writes == 1
    assert await mgr.save_state(PROVIDER) is False, "标记清掉后应恢复按需判定"


async def test_margin_configurable(tmp_path: Path) -> None:
    _write_state(tmp_path, time.time() + 3600)
    assert _manager(tmp_path, state_expiry_margin=60).state_needs_save(PROVIDER) is False


async def test_no_context_dont_crash(tmp_path: Path) -> None:
    mgr = _manager(tmp_path)
    mgr.mark_state_dirty(PROVIDER)  # 无 context（未创建）
    assert await mgr.save_state(PROVIDER) is False


# ---------- 关闭 ----------

async def test_stop_survives_dead_driver(tmp_path: Path, caplog) -> None:
    """Ctrl+C 打死驱动后：close 抛错也不能向上抛（否则 uvicorn 报 shutdown failed）。"""
    mgr = _manager(tmp_path)
    mgr._browser = StubBrowser(close_raises=True)  # type: ignore[assignment]
    mgr._playwright = StubPlaywright(stop_raises=True)  # type: ignore[assignment]
    with caplog.at_level(logging.WARNING):
        await mgr.stop()  # 不抛
    assert any("browser.close 失败" in r.message for r in caplog.records), caplog.text
    assert mgr._browser is None and mgr._playwright is None


async def test_stop_is_idempotent(tmp_path: Path) -> None:
    """stop() 后再调一次不应重复 close（否则第二次必然撞"transport closed"）。"""
    mgr = _manager(tmp_path)
    browser = StubBrowser()
    mgr._browser = browser  # type: ignore[assignment]
    mgr._playwright = StubPlaywright()  # type: ignore[assignment]
    await mgr.stop()
    await mgr.stop()
    assert browser.close_calls == 1 and browser.closed is True


async def test_stop_closes_contexts_first(tmp_path: Path) -> None:
    closed: list[str] = []

    class Ctx:
        def __init__(self, name: str):
            self.name = name

        async def close(self) -> None:
            closed.append(self.name)

    class BadCtx(Ctx):
        async def close(self) -> None:
            closed.append(f"{self.name}-attempt")
            raise RuntimeError("page crashed")

    mgr = _manager(tmp_path)
    mgr._contexts = {"a": Ctx("a"), "b": BadCtx("b")}  # type: ignore[dict-item]
    await mgr.stop()
    assert sorted(closed) == ["a", "b-attempt"] and mgr._contexts == {}
