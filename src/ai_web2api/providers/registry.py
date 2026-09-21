"""Provider 注册表：模型名 → Provider 路由。"""

from __future__ import annotations

import logging

from playwright.async_api import async_playwright

from ..browser.manager import BrowserManager
from ..config import AppConfig
from ..core.errors import ModelNotFoundError
from .base import BaseProvider, first_match
from .deepseek import DeepSeekProvider
from .qwen import QwenProvider

logger = logging.getLogger(__name__)

# 驱动类注册表：新增 Web AI 时在此登记
DRIVERS: dict[str, type[BaseProvider]] = {
    "deepseek": DeepSeekProvider,
    "qwen": QwenProvider,
}

# 常见 OpenAI 模型名（llama_index 等客户端默认使用）→ 自动映射到首选 provider 的默认模型。
# 这样客户端零配置即可接入；provider 配置里的 model_aliases 优先于这里的兜底。
OPENAI_COMMON_MODELS: tuple[str, ...] = (
    "gpt-4",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
    "gpt-3.5-turbo",
    "gpt-4.1",
    "gpt-4.1-mini",
    "o1",
    "o1-mini",
    "o3-mini",
    "o4-mini",
)


class ProviderRegistry:
    def __init__(self, config: AppConfig, browser: BrowserManager):
        self.config = config
        self._browser = browser
        self._providers: dict[str, BaseProvider] = {}
        self._model_map: dict[str, BaseProvider] = {}
        self._aliases: dict[str, str] = {}  # 别名 → 真实模型名（客户端兼容层）
        self._alias_owner: dict[str, str] = {}  # 别名 → provider 名（用于暴露列表）
        self._login_status: dict[str, bool] = {}

        for pcfg in config.providers:
            if not pcfg.enabled:
                continue
            driver = DRIVERS.get(pcfg.driver or pcfg.name)
            if driver is None:
                logger.warning("未知驱动 %r，provider %s 已跳过", pcfg.driver or pcfg.name, pcfg.name)
                continue
            provider = driver(pcfg, browser)
            self._providers[pcfg.name] = provider
            for m in pcfg.models:
                if m.name in self._model_map:
                    logger.warning(
                        "模型名 %r 在多个 provider 重复，%s 将覆盖先注册者",
                        m.name, pcfg.name,
                    )
                self._model_map[m.name] = provider
            for alias, target in pcfg.model_aliases.items():
                if target not in self._model_map:
                    logger.warning(
                        "provider %s 的别名 %r 指向不存在的模型 %r，已跳过",
                        pcfg.name, alias, target,
                    )
                    continue
                self._aliases[alias] = target
                self._alias_owner[alias] = pcfg.name
            logger.info("registered provider %s (models=%s)", pcfg.name, [m.name for m in pcfg.models])

        # 内置兜底：常见 OpenAI 模型名 → 默认 provider 的默认模型（显式别名优先，不覆盖）。
        # 默认 provider：server.default_provider（未配 = 第一个启用的 provider）。
        default_name = self.config.server.default_provider or next(iter(self._providers), None)
        if self.config.server.default_provider and default_name not in self._providers:
            logger.warning(
                "server.default_provider=%r 不存在/未启用，兜底别名不挂载",
                self.config.server.default_provider,
            )
        elif default_name:
            p = self._providers[default_name]
            default_model = p.exposed_models[0]
            for alias in OPENAI_COMMON_MODELS:
                if alias not in self._aliases and default_model in self._model_map:
                    self._aliases.setdefault(alias, default_model)
                    self._alias_owner.setdefault(alias, default_name)

    # ---------- 查询 ----------

    def get_provider(self, name: str) -> BaseProvider:
        p = self._providers.get(name)
        if p is None:
            raise ModelNotFoundError(f'provider "{name}" 不存在')
        return p

    def get_for_model(self, model: str) -> BaseProvider:
        p = self._model_map.get(model)
        if p is None:
            target = self._aliases.get(model)
            if target:
                p = self._model_map.get(target)
        if p is None:
            raise ModelNotFoundError(f'model "{model}" 不存在')
        return p

    def resolve_model_name(self, model: str) -> str:
        """请求模型名 → 实际驱动模型名（别名解析）。"""
        if model in self._model_map:
            return model
        return self._aliases.get(model, model)

    def list_models(self) -> list[dict]:
        out = []
        for name, p in self._providers.items():
            for m in p.exposed_models:
                out.append({"id": m, "object": "model", "created": 0, "owned_by": name})
        # 别名也暴露，客户端（llama_index 等）可校验到
        for alias, owner in self._alias_owner.items():
            out.append({"id": alias, "object": "model", "created": 0, "owned_by": owner})
        return out

    def providers(self) -> dict[str, BaseProvider]:
        return self._providers

    # ---------- 登录态 ----------

    def set_login_status(self, name: str, ok: bool) -> None:
        self._login_status[name] = ok

    def login_status(self) -> dict[str, bool]:
        return dict(self._login_status)

    async def refresh_login_status(self) -> None:
        if not self.config.browser.status_check:
            logger.info("定时状态检测已关闭（browser.status_check=false），跳过")
            return
        if self.config.browser.status_check_headless:
            await self._refresh_login_status_headless()
            return
        for name, p in self._providers.items():
            try:
                ok = await p.check_login()
            except Exception as e:  # noqa: BLE001
                logger.warning("check_login(%s) 失败: %s", name, e)
                ok = False
            self._login_status[name] = ok
            logger.info("login status %s: %s", name, ok)

    async def _refresh_login_status_headless(self) -> None:
        """用独立 headless 浏览器做定时状态检测（不占用/不弹出主浏览器窗口）。

        登录态取主浏览器 context 的实时 storage_state（context 不存在时回退
        state.json），逐 provider 打开页面检查 login_check 选择器。
        """
        from ..browser.manager import LAUNCH_ARGS

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=LAUNCH_ARGS,
            )
            try:
                for name, p in self._providers.items():
                    ok = False
                    try:
                        state = await self._browser.export_storage_state(name)
                        kwargs: dict = {}
                        if state is not None:
                            kwargs["storage_state"] = state
                        ctx = await browser.new_context(
                            user_agent=self.config.browser.user_agent,
                            locale=p.locale,
                            extra_http_headers={
                                "Accept-Language": BrowserManager._accept_language(p.locale)
                            },
                            **kwargs,
                        )
                        try:
                            page = await ctx.new_page()
                            await page.goto(p.cfg.url, wait_until="domcontentloaded", timeout=30000)
                            sel = await first_match(page, p.login_check_selectors)
                            if sel is not None:
                                await page.wait_for_selector(sel, timeout=8000)
                                ok = True
                        finally:
                            await ctx.close()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("headless check_login(%s) 失败: %s", name, e)
                    self._login_status[name] = ok
                    logger.info("login status %s: %s (headless)", name, ok)
            finally:
                await browser.close()
