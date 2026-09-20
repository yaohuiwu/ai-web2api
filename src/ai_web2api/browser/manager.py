"""Playwright 浏览器生命周期：单实例 + 每 Provider 一个 Context + storage_state 持久化。"""

from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    StorageState,
    async_playwright,
)

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

    @staticmethod
    def _accept_language(locale: str) -> str:
        """locale → Accept-Language（zh-CN → zh-CN,zh;q=0.9）。"""
        base = (locale or "").split("-")[0].strip()
        if not base or base == locale:
            return locale or "zh-CN"
        return f"{locale},{base};q=0.9"

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
            "locale": self._cfg.locale,
            # 显式 Accept-Language：页面 UI 语言（以及中文选择器）由它决定
            "extra_http_headers": {
                "Accept-Language": self._accept_language(self._cfg.locale)
            },
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

    async def export_storage_state(self, provider: str) -> StorageState | None:
        """导出 provider 当前登录态（供独立 headless 检测使用）。

        优先取主浏览器 context 的实时 storage_state；context 不存在时回退到
        落盘的 state.json（无登录态返回 None）。
        """
        ctx = self._contexts.get(provider)
        if ctx:
            try:
                return await ctx.storage_state()
            except Exception:
                pass
        path = self.state_path(provider)
        if path.exists():
            try:
                import json

                return json.loads(path.read_text())
            except Exception:
                pass
        return None

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

    async def open_page(self, provider: str, init_scripts: list[str] | None = None) -> Page:
        """新开页面（调用方随后 goto）。

        ``init_scripts``：provider 提供的页面级注入脚本（如 DeepSeek 的 XHR 网络
        监听），在 goto 之前注入（add_init_script 只对后续导航生效）。
        """
        ctx = await self.get_context(provider)
        page = await ctx.new_page()
        for script in init_scripts or []:
            await page.add_init_script(script)
        return page
