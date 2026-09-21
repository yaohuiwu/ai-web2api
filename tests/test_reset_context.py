"""BrowserManager.reset_context：导入 state 后立即生效（丢弃内存 context）。

运行：.venv/bin/python -m pytest tests/test_reset_context.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_web2api.browser.manager import BrowserManager
from ai_web2api.config import BrowserConfig


class _StubCtx:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_reset_context_closes_and_drops(tmp_path: Path):
    bm = BrowserManager(BrowserConfig(), tmp_path)
    ctx = _StubCtx()
    bm._contexts["qwen"] = ctx  # type: ignore[assignment]
    bm.mark_state_dirty("qwen")

    await bm.reset_context("qwen")
    assert ctx.closed is True
    assert "qwen" not in bm._contexts
    assert "qwen" not in bm._state_dirty  # 脏标记一并清掉（新 context 从盘上加载）


@pytest.mark.asyncio
async def test_reset_context_idempotent(tmp_path: Path):
    bm = BrowserManager(BrowserConfig(), tmp_path)
    await bm.reset_context("qwen")  # 不存在也不报错
