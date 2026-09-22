"""ai-web2api 服务入口。"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

load_dotenv()  # 从 cwd 向上查找 .env（本地开发；Docker 用 compose env_file）
if env_file := os.environ.get("AI_WEB2API_ENV_FILE"):
    load_dotenv(env_file, override=True)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import create_router
from .browser.manager import BrowserManager
from .config import load_config
from .core.auth_expiry import compute_for_state_file
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


def _lan_ip() -> str | None:
    """本机对外的 IP（UDP connect 只做路由选择，不发包）。拿不到就返回 None。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


# 启动时打印的入口：(显示名, 路径)
_ENDPOINTS = (
    ("管理界面", "/ui/"),
    ("Playground", "/ui/playground.html"),
    ("OpenAI API", "/v1"),
)


def usable_hosts(host: str) -> list[tuple[str, str]]:
    """把监听地址翻成浏览器真能打开的地址。

    `0.0.0.0`/`::` 只是"监听所有网卡"，不是主机名 —— 打印 http://0.0.0.0/ui 
    点不开，所以换成 127.0.0.1（本机）和局域网 IP（手机/其他机器）。
    """
    if host in {"0.0.0.0", "::", "*", ""}:
        hosts = [("127.0.0.1", "本机")]
        lan = _lan_ip()
        if lan:
            hosts.append((lan, "局域网"))
        return hosts
    if host == "::1":
        return [("[::1]", "本机")]
    return [(host, "本机")]


def log_ui_urls(host: str, port: int) -> None:
    """启动后打印可点击的地址（管理界面 / Playground / API）。

    一条日志一个地址：整行太长会在终端里折行，链接就没法直接点/复制了。
    """
    for h, tag in usable_hosts(host):
        base = f"http://{h}:{port}"
        for label, path in _ENDPOINTS:
            logger.info("%s（%s）：%s%s", label, tag, base, path)


def serve(cfg) -> None:  # type: ignore[no-untyped-def]
    """先自己 bind 再交给 uvicorn。

    端口被占用时给一句人话（而不是 uvicorn 的 traceback），也保证打印出来的链接
    一定真的能打开 —— uvicorn 是先跑 lifespan 再 bind 的，在 lifespan 里打印会撒谎。
    """
    family = socket.AF_INET6 if ":" in cfg.server.host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((cfg.server.host, cfg.server.port))
    except OSError as exc:
        raise SystemExit(
            f"启动失败：{cfg.server.host}:{cfg.server.port} 无法监听（{exc.strerror}），"
            f"端口可能已被占用"
        ) from exc
    log_ui_urls(cfg.server.host, cfg.server.port)
    uvicorn.Server(
        uvicorn.Config(app, host=cfg.server.host, port=cfg.server.port)
    ).run(sockets=[sock])


def create_app(config_path: str = CONFIG_PATH) -> FastAPI:
    cfg = load_config(config_path)
    browser = BrowserManager(cfg.browser, cfg.profiles_dir)
    registry = ProviderRegistry(cfg, browser)
    threads = ThreadManager(cfg.server, cfg.profiles_dir)

    async def _startup_login() -> None:
        """启动时的登录态检测 + 自动登录。

        绝不能放在 lifespan 的 yield 之前 await：那样 uvicorn 在登录完成前
        不对外服务，`/ui` 会一直连不上（登录可能几十秒）。
        """
        try:
            await registry.refresh_login_status()
            await _auto_login_missing(registry)
        except Exception:  # noqa: BLE001
            logger.exception("启动登录检查失败（服务继续运行）")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await browser.start()  # 快（~1s）；登录检查/自动登录放后台，先让 /ui 可用
        bg = asyncio.create_task(_background_loop())
        boot = asyncio.create_task(_startup_login())
        try:
            yield
        finally:
            bg.cancel()
            boot.cancel()
            # 关停阶段：Playwright 驱动可能已被 Ctrl+C 打掉，噪声日志降到 DEBUG
            try:
                asyncio.get_running_loop().set_exception_handler(_quiet_shutdown_exception_handler)
            except Exception:  # noqa: BLE001
                pass
            # 等后台任务真正结束：它还可能在用浏览器/独立 playwright 实例
            await asyncio.gather(bg, boot, return_exceptions=True)
            try:
                await threads.close_all()
            except Exception:  # noqa: BLE001
                logger.warning("关闭会话时出错（继续收尾）", exc_info=True)
            threads.shutdown()
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
    if cfg.server.api_keys:

        @app.middleware("http")
        async def _api_key_guard(request: Request, call_next):
            """可选网关鉴权：配置 server.api_keys 后 /v1/* 需带 Bearer key。

            只保护 /v1/*（OpenAI 兼容面）；/ui、/admin、/healthz 保持开放，
            本地管理界面无需先填 key（对外暴露请自行用反代限制 /admin）。
            """
            if request.url.path.startswith("/v1/"):
                token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
                if token not in cfg.server.api_keys:
                    return JSONResponse(
                        status_code=401,
                        content={
                            "error": {
                                "message": "Invalid API key",
                                "type": "authentication_error",
                                "code": 401,
                            }
                        },
                    )
            return await call_next(request)

        logger.info("API key 鉴权已启用（%d key，保护 /v1/*）", len(cfg.server.api_keys))
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
                name: {"logged_in": registry.login_status().get(name, False)}
                for name in registry.providers()
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
                browser.record_login_at(name)  # 自动登录成功 = 一次新登录
                logger.info('provider "%s" 自动登录成功', name)
            else:
                logger.warning(
                    'provider "%s" 自动登录失败: %s',
                    name,
                    result.get("reason", "未知原因"),
                )

    _auth_warned: dict[str, str] = {}  # provider → 上次已提醒的认证状态（避免刷屏）

    async def _warn_auth_expiry() -> None:
        """手动认证 provider 临近/已过期 → 记一次 WARNING。

        仅 ``login.mode == "manual"``（自动认证无需人工干预，不提醒）；
        状态变化时才记，避免每轮刷屏。
        """
        for name, p in registry.providers().items():
            if p.cfg.login.mode != "manual":
                _auth_warned.pop(name, None)
                continue
            state_file = Path(cfg.profiles_dir) / name / "state.json"
            info = compute_for_state_file(
                state_file,
                auth_cookies=p.cfg.login.auth_cookies,
                session_ttl_days=p.cfg.login.session_ttl_days,
                warn_days=p.cfg.login.expiry_warn_days or cfg.browser.auth_expiry_warn_days,
                login_at=browser.read_login_at(name),
            )
            if info.state in ("soon", "expired") and _auth_warned.get(name) != info.state:
                logger.warning(
                    'provider "%s" 认证%s（还剩 %.1f 天）：请更新认证信息 → '
                    "python -m ai_web2api.cli login %s",
                    name,
                    "已过期" if info.state == "expired" else "即将过期",
                    info.days_left or 0.0,
                    name,
                )
            _auth_warned[name] = info.state

    async def _background_loop():
        """定期刷新登录态 + （必要时）保存 storage_state + 回收空闲 thread 会话。"""
        while True:
            try:
                await asyncio.sleep(cfg.browser.login_check_interval)
                await _warn_auth_expiry()  # 独立于登录态刷新（读 state.json，刷新失败也要提醒）
                before = registry.login_status()
                await registry.refresh_login_status()
                # 掉线且配了自动登录凭据 → 自愈重登（模式=auto 且未登录时才会动作）
                await _auto_login_missing(registry)
                after = registry.login_status()
                for name in registry.providers():
                    if not after.get(name):
                        continue
                    if not before.get(name):
                        # 刚从不登录变登录（可能后台自动登录/页面恢复）→ 必须落盘
                        browser.mark_state_dirty(name)
                        browser.record_login_at(name)  # 记一次登录时刻（不随轮换变化）
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
    serve(app.state.config)
