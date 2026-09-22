"""cookie 轮换即落盘：会话型 cookie（无 expires）变化也要写 state.json。

运行：.venv/bin/python -m pytest tests/test_state_cookie_change.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_web2api.browser.manager import BrowserManager
from ai_web2api.config import BrowserConfig


class _Ctx:
    def __init__(self, cookies: list[dict], local_storage: list[tuple] | None = None) -> None:
        self._cookies = cookies
        self._ls = list(local_storage or [])
        self.saves = 0

    async def cookies(self) -> list[dict]:
        return list(self._cookies)

    async def storage_state(self, path: str | None = None) -> dict:
        if path is None:  # 指纹用：返回 cookies + localStorage
            return {
                "cookies": list(self._cookies),
                "origins": [
                    {"origin": o, "localStorage": [{"name": n, "value": v}]} for o, n, v in self._ls
                ],
            }
        self.saves += 1  # 落盘用
        Path(path).write_text("{}", encoding="utf-8")
        return {}


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
    bm._state_fp["p"] = await bm._state_fingerprint("p")

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
    bm._state_fp["p"] = await bm._state_fingerprint("p")
    assert await bm.save_state("p", force=True) is True
    assert ctx.saves == 1


@pytest.mark.asyncio
async def test_reset_context_clears_fingerprint(tmp_path: Path):
    bm = BrowserManager(BrowserConfig(), tmp_path)
    _ready(bm, [{"name": "sid", "value": "1"}])
    bm._state_fp["p"] = "x"
    await bm.reset_context("p")
    assert "p" not in bm._state_fp


@pytest.mark.asyncio
async def test_saves_when_local_storage_token_rotates(tmp_path: Path):
    """localStorage 里的 token 轮换（Kimi/DeepSeek）也必须落盘。

    只比较 cookies 会漏掉这种情况 → 重启后用的是过期快照。
    """
    bm = BrowserManager(BrowserConfig(), tmp_path)
    ctx = _ready(bm, [{"name": "sid", "value": "1"}])
    ctx._ls = [("https://www.kimi.com", "refresh_token", "old")]
    bm._state_fp["p"] = await bm._state_fingerprint("p")

    assert await bm.save_state("p") is False          # 无变化
    ctx._ls = [("https://www.kimi.com", "refresh_token", "new-token")]   # 轮换
    assert await bm.save_state("p") is True
    assert ctx.saves == 1
    assert await bm.save_state("p") is False          # 指纹已更新
