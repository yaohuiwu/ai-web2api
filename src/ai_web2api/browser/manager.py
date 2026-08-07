"""Playwright 浏览器生命周期：单实例 + 每 Provider 一个 Context + storage_state 持久化。"""

from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from ..config import BrowserConfig

logger = logging.getLogger(__name__)

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
]


class BrowserManager:
    """管理一个 Chromium 实例。

    每个 Provider 拥有一个独立 Context（独立登录态），登录态通过
    ``storage_state`` 落盘到 ``profiles/<provider>/state.json``，重启后自动恢复。
    """

    def __init__(self, browser_cfg: BrowserConfig, profiles_dir: Path):
        self._cfg = browser_cfg
        self._profiles_dir = profiles_dir
        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = None
        self._browser: Browser | None = None
        self._contexts: dict[str, BrowserContext] = {}

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._cfg.headless,
            args=LAUNCH_ARGS,
        )
        logger.info("browser started (headless=%s)", self._cfg.headless)

    async def stop(self) -> None:
        for ctx in self._contexts.values():
            try:
                await ctx.close()
            except Exception:
                pass
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("browser stopped")

    # ---------- Context ----------

    def state_path(self, provider: str) -> Path:
        return self._profiles_dir / provider / "state.json"

    async def get_context(self, provider: str) -> BrowserContext:
        if provider in self._contexts:
            return self._contexts[provider]
        assert self._browser is not None, "browser not started"
        state = self.state_path(provider)
        kwargs: dict = {
            "user_agent": self._cfg.user_agent,
            "viewport": self._cfg.viewport,
        }
        if state.exists():
            kwargs["storage_state"] = str(state)
        ctx = await self._browser.new_context(**kwargs)
        ctx.set_default_timeout(self._cfg.default_timeout * 1000)
        self._contexts[provider] = ctx
        logger.info("context created for provider %s (state restored=%s)", provider, state.exists())
        return ctx

    async def save_state(self, provider: str) -> None:
        ctx = self._contexts.get(provider)
        if not ctx:
            return
        path = self.state_path(provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(path))
        logger.info("state saved for provider %s", provider)

    async def clear_state(self, provider: str) -> None:
        ctx = self._contexts.get(provider)
        if ctx:
            try:
                await ctx.clear_cookies()
            except Exception:
                pass
        path = self.state_path(provider)
        if path.exists():
            path.unlink()

    async def open_page(self, provider: str) -> Page:
        ctx = await self.get_context(provider)
        return await ctx.new_page()
