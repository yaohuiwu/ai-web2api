"""cookie 轮换即落盘：会话型 cookie（无 expires）变化也要写 state.json。

运行：.venv/bin/python -m pytest tests/test_state_cookie_change.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_web2api.browser.manager import BrowserManager
from ai_web2api.config import BrowserConfig


class _Ctx:
    def __init__(self, cookies: list[dict]) -> None:
        self._cookies = cookies
        self.saves = 0

    async def cookies(self) -> list[dict]:
        return list(self._cookies)

    async def storage_state(self, path: str) -> None:
        self.saves += 1
        Path(path).write_text("{}", encoding="utf-8")


def _ready(bm: BrowserManager, cookies: list[dict]) -> _Ctx:
    """预置 state 文件 + 基线指纹（模拟已登录且未过期）。"""
    p = bm.state_path("p")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    ctx = _Ctx(cookies)
    bm._contexts["p"] = ctx  # type: ignore[assignment]
    return ctx


@pytest.mark.asyncio
async def test_saves_only_when_cookies_change(tmp_path: Path):
    bm = BrowserManager(BrowserConfig(), tmp_path)
    ctx = _ready(bm, [{"name": "sid", "value": "1"}])
    bm._cookie_fp["p"] = await bm._cookie_fingerprint("p")

    # 无变化 → 不写（旧规则：文件在、无 expiry、无脏标记 → 跳过）
    assert await bm.save_state("p") is False
    assert ctx.saves == 0

    # cookie 轮换（会话 token 变了）→ 写盘
    ctx._cookies = [{"name": "sid", "value": "2"}]
    assert await bm.save_state("p") is True
    assert ctx.saves == 1

    # 写完指纹更新，再调无变化 → 不写
    assert await bm.save_state("p") is False
    assert ctx.saves == 1


@pytest.mark.asyncio
async def test_force_always_saves(tmp_path: Path):
    bm = BrowserManager(BrowserConfig(), tmp_path)
    ctx = _ready(bm, [{"name": "sid", "value": "1"}])
    bm._cookie_fp["p"] = await bm._cookie_fingerprint("p")
    assert await bm.save_state("p", force=True) is True
    assert ctx.saves == 1


@pytest.mark.asyncio
async def test_reset_context_clears_fingerprint(tmp_path: Path):
    bm = BrowserManager(BrowserConfig(), tmp_path)
    _ready(bm, [{"name": "sid", "value": "1"}])
    bm._cookie_fp["p"] = "x"
    await bm.reset_context("p")
    assert "p" not in bm._cookie_fp
