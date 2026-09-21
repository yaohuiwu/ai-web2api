"""Playwright 浏览器生命周期：单实例 + 每 Provider 一个 Context + storage_state 持久化。"""

from __future__ import annotations

import json
import logging
import time
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
        # 登录态"变了"的标记（登录成功/注入 cookie）→ 下次 save_state 必须落盘
        self._state_dirty: set[str] = set()

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._cfg.headless,
            args=LAUNCH_ARGS,
        )
        logger.info("browser started (headless=%s)", self._cfg.headless)

    async def stop(self) -> None:
        """关闭浏览器与 Playwright 驱动。

        尽力而为：Ctrl+C 会把**整个进程组**（含 Playwright 的 node 驱动）一起打掉，
        此时传输已关闭，``browser.close()`` 必然抛 RuntimeError —— 那不是错误，
        只是没机会优雅收尾，不能让它把 uvicorn 的 shutdown 打成 failed。
        """
        for name, ctx in list(self._contexts.items()):
            try:
                await ctx.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("context close skipped for %s: %s", name, exc)
        self._contexts.clear()
        browser, self._browser = self._browser, None
        if browser is not None:
            try:
                await browser.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("browser.close 失败（驱动可能已退出，忽略）：%s", exc)
        pw, self._playwright = self._playwright, None
        if pw is not None:
            try:
                await pw.stop()
            except Exception as exc:  # noqa: BLE001
                logger.debug("playwright.stop 失败（忽略）：%s", exc)
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

    # ---------- 登录失败截图（webui 调试用） ----------

    def login_error_path(self, provider: str) -> Path:
        return self._profiles_dir / provider / "login_error.png"

    async def save_login_error(self, provider: str, page: Page) -> str | None:
        """登录失败时抓页面截图（供 webui 展示，定位风控/验证码/改版）。"""
        path = self.login_error_path(provider)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path))
            logger.info("login error screenshot saved: %s", path)
            return str(path)
        except Exception:  # noqa: BLE001
            logger.warning(
                "save login error screenshot failed (provider=%s)", provider, exc_info=True
            )
            return None

    def login_error_mtime(self, provider: str) -> float | None:
        path = self.login_error_path(provider)
        try:
            return path.stat().st_mtime if path.exists() else None
        except Exception:  # noqa: BLE001
            return None

    def clear_login_error(self, provider: str) -> None:
        try:
            self.login_error_path(provider).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    @property
    def default_locale(self) -> str:
        return self._cfg.locale

    @property
    def user_agent(self) -> str:
        return self._cfg.user_agent

    async def get_context(self, provider: str, locale: str | None = None) -> BrowserContext:
        if provider in self._contexts:
            return self._contexts[provider]
        assert self._browser is not None, "browser not started"
        state = self.state_path(provider)
        loc = locale or self._cfg.locale
        kwargs: dict = {
            "user_agent": self._cfg.user_agent,
            "viewport": self._cfg.viewport,
            "locale": loc,
            # 显式 Accept-Language：页面 UI 语言（以及中文选择器）由它决定
            "extra_http_headers": {
                "Accept-Language": self._accept_language(loc)
            },
        }
        if state.exists():
            kwargs["storage_state"] = str(state)
        ctx = await self._browser.new_context(**kwargs)
        ctx.set_default_timeout(self._cfg.default_timeout * 1000)
        self._contexts[provider] = ctx
        logger.info("context created for provider %s (state restored=%s)", provider, state.exists())
        return ctx

    def mark_state_dirty(self, provider: str) -> None:
        """标记登录态已变化（刚登录成功 / 注入了 cookie）→ 下次 save_state 必须落盘。"""
        self._state_dirty.add(provider)

    def state_expiry(self, provider: str) -> float | None:
        """state.json 中"最早到期"的 cookie 到期时间（epoch 秒）。

        没有可判定的到期时间（无文件/无 cookie/全是会话 cookie）→ None。
        """
        path = self.state_path(provider)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except Exception:
            return None
        expiries = [
            float(c["expires"])
            for c in data.get("cookies", [])
            if isinstance(c.get("expires"), (int, float)) and float(c["expires"]) > 0
        ]
        return min(expiries) if expiries else None

    def state_needs_save(self, provider: str, *, now: float | None = None) -> bool:
        """是否**有必要**写 state.json。

        规则：已登录且登录态未过期就不写（省掉无意义的定期落盘/磁盘抖动）。
        需要写的情形只有三种：
        1. 登录态刚变化（mark_state_dirty：登录成功 / 注入 cookie）；
        2. 还没有落盘文件（首次登录后必须持久化）；
        3. 最早到期的 cookie 已在 ``state_expiry_margin`` 内（快过期，趁机刷新）。
        """
        if provider in self._state_dirty:
            return True
        path = self.state_path(provider)
        if not path.exists():
            return True
        expiry = self.state_expiry(provider)
        if expiry is None:
            return False  # 会话 cookie：没有"过期"可言，登录态没变就不必写
        now = time.time() if now is None else now
        return (expiry - now) < self._cfg.state_expiry_margin

    async def save_state(self, provider: str, *, force: bool = False) -> bool:
        """写 storage_state（默认只在该写的时候写，返回是否真的写了）。

        ``force=True`` 用于调用方明确知道变了、必须落盘的场合（如刚注入 cookie）。
        """
        if not force and not self.state_needs_save(provider):
            logger.debug("state save skipped for provider %s（已登录且未过期）", provider)
            return False
        ctx = self._contexts.get(provider)
        if not ctx:
            return False
        path = self.state_path(provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(path))
        self._state_dirty.discard(provider)
        logger.info("state saved for provider %s", provider)
        return True

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

    async def open_page(
        self,
        provider: str,
        init_scripts: list[str] | None = None,
        locale: str | None = None,
    ) -> Page:
        """新开页面（调用方随后 goto）。

        ``init_scripts``：provider 提供的页面级注入脚本（如 DeepSeek 的 XHR 网络
        监听），在 goto 之前注入（add_init_script 只对后续导航生效）。
        ``locale``：provider 级语言覆盖（空 = 全局 browser.locale）。
        """
        ctx = await self.get_context(provider, locale=locale)
        page = await ctx.new_page()
        for script in init_scripts or []:
            await page.add_init_script(script)
        return page
