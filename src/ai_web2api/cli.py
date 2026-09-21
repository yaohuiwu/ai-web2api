"""ai-web2api 命令行（运维子命令）。

用法：
    python -m ai_web2api.cli login qwen            # 有头浏览器手动登录 → 自动导入本地服务
    python -m ai_web2api.cli login deepseek --manual
    python -m ai_web2api.cli login qwen --no-import

设计见 docs/MANUAL_LOGIN.md。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import urllib.request
from pathlib import Path

from .browser import extractor
from .browser.manager import BrowserManager
from .config import AppConfig, load_config
from .providers.registry import ProviderRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ai_web2api.cli", description="ai-web2api 运维命令")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="手动登录（有头浏览器）并导出/导入登录态")
    p.add_argument("provider", nargs="?", help="provider 名（省略 = server.default_provider / 首个启用）")
    p.add_argument("--config", default=os.environ.get("AI_WEB2API_CONFIG", "config.yaml"))
    p.add_argument("--manual", action="store_true", help="不自动填表，纯手动")
    p.add_argument("--timeout", type=float, default=600.0, help="等待登录成功的最长秒数")
    p.add_argument("--out", default=None, help="state 输出路径（默认 profiles/<p>/state.json）")
    p.add_argument("--import-url", dest="import_url", default=None, help="导入地址（默认本地服务）")
    p.add_argument("--import-key", dest="import_key", default=os.environ.get("WEB2API_API_KEY"))
    p.add_argument("--import", dest="import_state", action="store_true", help="登录后导入服务（默认）")
    p.add_argument("--no-import", dest="import_state", action="store_false", help="不导入，只写 state")
    p.set_defaults(import_state=True)
    return parser


def _local_import_url(cfg: AppConfig) -> str:
    """由 config 推导本地服务地址（0.0.0.0 → 127.0.0.1）。"""
    host = (cfg.server.host or "127.0.0.1").strip()
    if host in {"0.0.0.0", "::", "*", ""}:
        host = "127.0.0.1"
    return f"http://{host}:{cfg.server.port}"


def _post_state(url: str, provider: str, state: dict, api_key: str | None) -> tuple[bool, str]:
    data = json.dumps(state).encode()
    req = urllib.request.Request(
        f"{url.rstrip('/')}/admin/{provider}/login/state",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True, r.read().decode()[:200]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


async def _type_into(loc, text: str) -> None:
    """逐字输入（React 受控组件 fill() 可能不生效）。"""
    await loc.click()
    await loc.fill("")
    await loc.press_sequentially(text, delay=25)


async def _login_flow(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    browser = BrowserManager(
        cfg.browser.model_copy(update={"headless": False}), cfg.profiles_dir
    )
    await browser.start()
    try:
        registry = ProviderRegistry(cfg, browser)
        names = list(registry.providers())
        name = args.provider or cfg.server.default_provider or (names[0] if names else None)
        if not name or name not in registry.providers():
            print(f"未知 provider：{name!r}；可用：{names}")
            return 2
        provider = registry.get_provider(name)
        lp = provider.cfg.login.page
        ctx = await browser.get_context(name, locale=provider.locale)
        page = await browser.open_page(
            name, init_scripts=provider.init_scripts(), locale=provider.locale
        )
        try:
            sel = await provider._goto_ready(
                page, provider.login_url, provider.login_check_selectors, total_timeout=20.0
            )
            if sel is not None:
                print(f"[{name}] 已是登录状态。")
            else:
                # 等加载遮罩 + 切「密码登录」tab
                if not await provider._page_stuck_loading(page):
                    await provider._wait_loading_gone(page, lp)
                    for _ in range(2):
                        if await extractor.first_match(page, lp.password) is not None:
                            break
                        tab = await extractor.first_match(page, lp.password_tab)
                        if tab is None:
                            break
                        try:
                            await page.locator(tab).first.click()
                        except Exception:  # noqa: BLE001
                            pass
                        await extractor.wait_first_match(page, lp.password, timeout=6.0)
                # 非 --manual：自动填账号密码
                if not args.manual:
                    creds = provider.get_credentials()
                    if creds["username"] and creds["password"]:
                        u = await extractor.wait_first_match(page, lp.username, timeout=8.0)
                        p = await extractor.wait_first_match(page, lp.password, timeout=8.0)
                        if u and p:
                            await _type_into(page.locator(u).first, creds["username"])
                            await _type_into(page.locator(p).first, creds["password"])
                            print(f"[{name}] 已自动填入账号密码（验证码/Google 请手动完成）。")
                print(
                    f"[{name}] 请在弹出的浏览器窗口完成登录（Google / 验证码 / 短信均可），"
                    f"等待中…（最长 {args.timeout:.0f}s，Ctrl+C 取消）"
                )
                deadline = time.monotonic() + args.timeout
                ok = False
                while time.monotonic() < deadline:
                    if (
                        await extractor.first_match(page, provider.login_check_selectors)
                        is not None
                    ):
                        ok = True
                        break
                    await page.wait_for_timeout(2000)
                if not ok:
                    print(f"[{name}] 超时未检测到登录成功，未保存。")
                    return 1

            # 保存 storage_state
            out = Path(args.out) if args.out else browser.state_path(name)
            out.parent.mkdir(parents=True, exist_ok=True)
            await ctx.storage_state(path=str(out))
            print(f"[{name}] 登录态已保存：{out}")

            # 导入（默认开）
            if args.import_state:
                url = args.import_url or _local_import_url(cfg)
                state = json.loads(out.read_text(encoding="utf-8"))
                okk, msg = _post_state(url, name, state, args.import_key)
                if okk:
                    print(f"[{name}] 已导入 {url}：{msg}")
                else:
                    print(
                        f"[{name}] 导入 {url} 失败（{msg}）。\n"
                        f"        可稍后手动导入：POST {url}/admin/{name}/login/state（body = {out}）"
                    )
            return 0
        finally:
            await page.close()
    finally:
        await browser.stop()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_login_flow(args))
    except KeyboardInterrupt:
        print("\n已取消（未保存/未导入）。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
