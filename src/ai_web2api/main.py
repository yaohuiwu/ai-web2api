"""ai-web2api 服务入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# 项目根 .env（自动登录凭据 DEEPSEEK_USERNAME/DEEPSEEK_PASSWORD 等）
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
load_dotenv()

from .api.routes import create_router
from .browser.manager import BrowserManager
from .config import load_config
from .core.errors import ProviderError
from .core.threads import ThreadManager
from .providers.registry import ProviderRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("ai_web2api")

CONFIG_PATH = os.environ.get("AI_WEB2API_CONFIG", "config.yaml")

# 收尾噪声：Ctrl+C 把 Playwright 的 node 驱动一起打掉后，它内部 future 会抛这些异常，
# 都属于"驱动已经不在了"，不影响收尾（登录态该落盘的早落了）。
_CLOSED_HINTS = (
    "Connection closed while reading from the driver",
    "Target page, context or browser has been closed",
    "unable to perform operation on <WriteUnixTransport closed=True",
)


def _quiet_shutdown_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """关停阶段把 Playwright 的"连接已关闭"类未取回异常降到 DEBUG，其余照旧。"""
    exc = context.get("exception")
    text = f"{context.get('message', '')} · {exc!r}"
    if any(h in text for h in _CLOSED_HINTS):
        logger.debug("忽略 Playwright 收尾异常：%s", text[:200])
        return
    loop.default_exception_handler(context)


def create_app(config_path: str = CONFIG_PATH) -> FastAPI:
    cfg = load_config(config_path)
    browser = BrowserManager(cfg.browser, cfg.profiles_dir)
    registry = ProviderRegistry(cfg, browser)
    threads = ThreadManager(cfg.server, registry, cfg.profiles_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await browser.start()
        await registry.refresh_login_status()
        await _auto_login_missing(registry)
        bg = asyncio.create_task(_background_loop())
        try:
            yield
        finally:
            bg.cancel()
            # 关停阶段：Playwright 驱动可能已被 Ctrl+C 打掉，噪声日志降到 DEBUG
            try:
                asyncio.get_running_loop().set_exception_handler(_quiet_shutdown_exception_handler)
            except Exception:  # noqa: BLE001
                pass
            # 等后台任务真正结束：它还可能在用浏览器/独立 playwright 实例
            await asyncio.gather(bg, return_exceptions=True)
            try:
                await threads.close_all()
            except Exception:  # noqa: BLE001
                logger.warning("关闭会话时出错（继续收尾）", exc_info=True)
            try:
                await browser.stop()
            except Exception:  # noqa: BLE001
                logger.warning("关闭浏览器时出错（继续收尾）", exc_info=True)

    app = FastAPI(title="ai-web2api", version="0.1.0", lifespan=lifespan)
    if cfg.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.server.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(create_router(registry, threads))
    app.state.registry = registry
    app.state.browser = browser
    app.state.threads = threads
    app.state.config = cfg
    app.state.started_at = time.monotonic()

    # Web 管理界面：状态面板 + OpenAI API 测试页
    webui_dir = Path(__file__).resolve().parent / "webui"
    if webui_dir.is_dir():

        class _NoStoreStatic(StaticFiles):
            """给 /ui 的静态文件加 no-store：改完页面普通刷新即生效。

            没有缓存头时浏览器会启发式缓存 playground.html —— 页面开着不刷新
            就永远跑旧 JS，改动静默失效（表现为"改了没用"）。
            """

            async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
                resp = await super().get_response(path, scope)
                resp.headers["Cache-Control"] = "no-store, must-revalidate"
                return resp

        app.mount("/ui", _NoStoreStatic(directory=webui_dir, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        async def root():
            return RedirectResponse(url="/ui/")
    else:
        logger.warning("webui 目录不存在（%s），/ui 界面未挂载", webui_dir)

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

    async def _auto_login_missing(registry: ProviderRegistry) -> None:
        """启动时自动登录：未登录 + login.mode=auto + .env 有凭据 → 尝试自动登录。

        失败不阻塞启动（可能触发验证码/风控，留待手动登录），仅记日志。
        """
        for name in registry.providers():
            if registry.login_status().get(name):
                continue  # 已有登录态（state.json 恢复）
            provider = registry.get_provider(name)
            if provider.cfg.login.mode != "auto":
                continue
            creds = provider.get_credentials()
            if not creds["username"] or not creds["password"]:
                logger.warning(
                    'provider "%s" login.mode=auto 但未配置凭据键名（%s/%s），跳过自动登录',
                    name,
                    provider.cfg.login.username_env,
                    provider.cfg.login.password_env,
                )
                continue
            logger.info('provider "%s" 未登录，尝试自动登录…', name)
            try:
                result = await provider.gate.run(provider.auto_login)
            except Exception:
                logger.exception('provider "%s" 自动登录异常，请手动登录', name)
                continue
            if result.get("ok"):
                registry.set_login_status(name, True)
                logger.info('provider "%s" 自动登录成功', name)
            else:
                logger.warning(
                    'provider "%s" 自动登录失败: %s',
                    name,
                    result.get("reason", "未知原因"),
                )

    async def _background_loop():
        """定期刷新登录态 + （必要时）保存 storage_state + 回收空闲 thread 会话。"""
        while True:
            try:
                await asyncio.sleep(cfg.browser.login_check_interval)
                before = registry.login_status()
                await registry.refresh_login_status()
                after = registry.login_status()
                for name in registry.providers():
                    if not after.get(name):
                        continue
                    if not before.get(name):
                        # 刚从不登录变登录（可能后台自动登录/页面恢复）→ 必须落盘
                        browser.mark_state_dirty(name)
                    # 已登录且登录态未过期时这里什么都不写（save_state 内部判定）
                    await browser.save_state(name)
                await threads.cleanup()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("background loop error")

    return app


app = create_app()


if __name__ == "__main__":
    cfg = app.state.config
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port)
