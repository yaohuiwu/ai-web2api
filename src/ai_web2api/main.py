"""ai-web2api 服务入口。"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routes import create_router
from .browser.manager import BrowserManager
from .config import load_config
from .core.errors import ProviderError
from .providers.registry import ProviderRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ai_web2api")

CONFIG_PATH = os.environ.get("AI_WEB2API_CONFIG", "config.yaml")


def create_app(config_path: str = CONFIG_PATH) -> FastAPI:
    cfg = load_config(config_path)
    browser = BrowserManager(cfg.browser, cfg.profiles_dir)
    registry = ProviderRegistry(cfg, browser)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await browser.start()
        await registry.refresh_login_status()
        bg = asyncio.create_task(_background_loop())
        try:
            yield
        finally:
            bg.cancel()
            await browser.stop()

    app = FastAPI(title="ai-web2api", version="0.1.0", lifespan=lifespan)
    if cfg.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.server.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(create_router(registry))
    app.state.registry = registry
    app.state.browser = browser
    app.state.config = cfg

    @app.exception_handler(ProviderError)
    async def _provider_error_handler(request, exc: ProviderError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "message": exc.message,
                    "type": exc.error_type,
                    "code": exc.status_code,
                }
            },
        )

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "providers": {
                name: {"logged_in": ok} for name, ok in registry.login_status().items()
            },
        }

    async def _background_loop():
        """定期刷新登录态 + 定期保存 storage_state（防登录态过期丢失）。"""
        while True:
            try:
                await asyncio.sleep(cfg.browser.login_check_interval)
                await registry.refresh_login_status()
                for name in registry.providers():
                    if registry.login_status().get(name):
                        await browser.save_state(name)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("background loop error")

    return app


app = create_app()


if __name__ == "__main__":
    cfg = app.state.config
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port)
