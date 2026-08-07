"""Provider 注册表：模型名 → Provider 路由。"""

from __future__ import annotations

import logging

from ..browser.manager import BrowserManager
from ..config import AppConfig
from ..core.errors import ModelNotFoundError
from .base import BaseProvider
from .deepseek import DeepSeekProvider

logger = logging.getLogger(__name__)

# 驱动类注册表：新增 Web AI 时在此登记
DRIVERS: dict[str, type[BaseProvider]] = {
    "deepseek": DeepSeekProvider,
}


class ProviderRegistry:
    def __init__(self, config: AppConfig, browser: BrowserManager):
        self._providers: dict[str, BaseProvider] = {}
        self._model_map: dict[str, BaseProvider] = {}
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
                self._model_map[m.name] = provider
            logger.info("registered provider %s (models=%s)", pcfg.name, [m.name for m in pcfg.models])

    # ---------- 查询 ----------

    def get_provider(self, name: str) -> BaseProvider:
        p = self._providers.get(name)
        if p is None:
            raise ModelNotFoundError(f'provider "{name}" 不存在')
        return p

    def get_for_model(self, model: str) -> BaseProvider:
        p = self._model_map.get(model)
        if p is None:
            raise ModelNotFoundError(f'model "{model}" 不存在')
        return p

    def list_models(self) -> list[dict]:
        out = []
        for name, p in self._providers.items():
            for m in p.exposed_models:
                out.append({"id": m, "object": "model", "created": 0, "owned_by": name})
        return out

    def providers(self) -> dict[str, BaseProvider]:
        return self._providers

    # ---------- 登录态 ----------

    def set_login_status(self, name: str, ok: bool) -> None:
        self._login_status[name] = ok

    def login_status(self) -> dict[str, bool]:
        return dict(self._login_status)

    async def refresh_login_status(self) -> None:
        for name, p in self._providers.items():
            try:
                ok = await p.check_login()
            except Exception as e:  # noqa: BLE001
                logger.warning("check_login(%s) 失败: %s", name, e)
                ok = False
            self._login_status[name] = ok
            logger.info("login status %s: %s", name, ok)
