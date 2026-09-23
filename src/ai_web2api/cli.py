"""ai-web2api 命令行（运维子命令）。

用法（安装后可用短命令；``python -m ai_web2api.cli`` 等价）：
    ai-web2api login                 # 交互选择 provider → 有头浏览器登录 → 自动导入
    ai-web2api login chatgpt         # 指定 provider
    ai-web2api login deepseek --manual
    ai-web2api providers             # 列出 provider（模式/登录态/认证有效期）

导入地址优先级：``--import-url`` > ``AI_WEB2API_URL`` > 由 config 推导（0.0.0.0 → 127.0.0.1）。

设计见 docs/MANUAL_LOGIN.md。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .browser import extractor
from .browser.manager import BrowserManager
from .config import AppConfig, load_config
from .core.auth_expiry import compute_for_state_file
from .providers.base import js_type
from .providers.registry import ProviderRegistry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-web2api", description="ai-web2api 运维命令（也可用 python -m ai_web2api.cli）"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("providers", help="列出 provider（模式 / 登录态 / 认证有效期）")
    pp.add_argument("--config", default=os.environ.get("AI_WEB2API_CONFIG", "config.yaml"))

    p = sub.add_parser("login", help="手动登录（有头浏览器）并导出/导入登录态")
    p.add_argument("provider", nargs="?", help="provider 名（省略 = server.default_provider / 首个启用）")
    p.add_argument("--config", default=os.environ.get("AI_WEB2API_CONFIG", "config.yaml"))
    p.add_argument("--manual", action="store_true", help="不自动填表，纯手动")
    p.add_argument("--force-login", dest="force_login", action="store_true",
                   help="即使看起来已登录/可用，也强制走登录流程（游客态站点用）")
    p.add_argument("--timeout", type=float, default=600.0, help="等待登录成功的最长秒数")
    p.add_argument("--out", default=None, help="state 输出路径（默认 profiles/<p>/state.json）")
    p.add_argument(
        "--import-url",
        dest="import_url",
        default=os.environ.get("AI_WEB2API_URL"),
        help="导入地址（默认 AI_WEB2API_URL 环境变量，或由 config 推导的本地服务）",
    )
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


def _auth_summary(cfg: AppConfig, provider, browser: BrowserManager) -> str:
    """一行摘要：登录态 / 认证有效期（供交互选择与 `providers` 子命令复用）。"""
    info = compute_for_state_file(
        browser.state_path(provider.name),
        auth_cookies=provider.cfg.login.auth_cookies,
        session_ttl_days=provider.cfg.login.session_ttl_days,
        warn_days=provider.cfg.login.expiry_warn_days or cfg.browser.auth_expiry_warn_days,
        login_at=browser.read_login_at(provider.name),
    )
    st = info.state
    if st == "ok":
        tail = f"认证还剩 {info.days_left:.0f} 天"
    elif st == "soon":
        tail = f"⚠ 认证 {info.days_left:.0f} 天后过期"
    elif st == "expired":
        tail = "⚠ 认证已过期"
    else:
        tail = "认证有效期未知"
    return tail


def _pick_provider(cfg: AppConfig, registry: ProviderRegistry, names: list[str], given: str | None) -> str | None:
    """省略 provider 时：TTY 下交互选择，否则回退默认值。"""
    if given:
        if given in names:
            return given
        print(f"未知 provider：{given!r}\n可用：{', '.join(names)}")
        return None
    default = cfg.server.default_provider or (names[0] if names else None)
    if not names:
        print("没有启用的 provider（检查 config.yaml 的 providers.*.enabled）")
        return None
    if not sys.stdin.isatty():
        print(f"未指定 provider，使用默认值 {default!r}（可用：{', '.join(names)}）")
        return default
    print("请选择要登录的 provider：")
    for i, n in enumerate(names, 1):
        p = registry.get_provider(n)
        mark = "  ← 默认" if n == default else ""
        print(f"  {i}. {n:10s} [{p.cfg.login.mode:6s}] {_auth_summary(cfg, p, p.browser)}{mark}")
    raw = input(f"输入序号或名字（回车 = {default}）：").strip()
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(names):
        return names[int(raw) - 1]
    if raw in names:
        return raw
    print(f"输入无效：{raw!r}")
    return None


def _cmd_providers(cfg: AppConfig) -> int:
    """列出 provider（不需要浏览器：只读 state.json / login.json）。"""
    rows = [p for p in cfg.providers if p.enabled]
    if not rows:
        print("没有启用的 provider")
        return 1
    print(f"{'provider':12s} {'mode':7s} {'state.json':11s} 认证有效期")
    for p in rows:
        state = Path(cfg.profiles_dir) / p.name / "state.json"
        meta = Path(cfg.profiles_dir) / p.name / "login.json"
        login_at = None
        try:
            login_at = float(json.loads(meta.read_text(encoding="utf-8"))["login_at"])
        except Exception:  # noqa: BLE001
            pass
        info = compute_for_state_file(
            state,
            auth_cookies=p.login.auth_cookies,
            session_ttl_days=p.login.session_ttl_days,
            warn_days=p.login.expiry_warn_days or cfg.browser.auth_expiry_warn_days,
            login_at=login_at,
        )
        if info.state == "ok" and info.days_left is not None:
            detail = f"还剩 {info.days_left:.0f} 天"
        elif info.state == "soon" and info.days_left is not None:
            detail = f"⚠ {info.days_left:.0f} 天后过期"
        elif info.state == "expired":
            detail = "⚠ 已过期"
        else:
            detail = "未知（会话型/未配置）"
        print(f"{p.name:12s} {p.login.mode:7s} {('有' if state.exists() else '无'):11s} {detail}")
    return 0


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
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        hint = ""
        if e.code == 404:
            hint = (
                "  ← provider 未注册：确认 config.yaml 里 providers.<name>.enabled: true，"
                "并重启服务（Docker 需 docker compose up -d --build，config 是打进镜像的）"
            )
        return False, f"HTTP {e.code} {e.reason}{' ' + body if body else ''}{hint}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


async def _type_into(loc, text: str) -> None:
    """JS 注入输入（React 受控组件 fill() 可能不生效），委托给 js_type。"""
    await loc.click()
    await js_type(await loc.element_handle(), text)


async def _login_flow(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    browser = BrowserManager(
        cfg.browser.model_copy(update={"headless": False}), cfg.profiles_dir
    )
    # 先选 provider（只读配置，不启动浏览器 → 选错也不会弹窗）
    registry = ProviderRegistry(cfg, browser)
    name = _pick_provider(cfg, registry, list(registry.providers()), args.provider)
    if not name:
        return 2
    await browser.start()
    try:
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
            # 只有**配置了"登录标记"**（login_check 非空）且命中时才认为已登录：
            # 否则游客态站点（如 Gemini 游客可用、豆包游客也有输入框）会命中输入框而被误判，
            # 用户就跑不了手动登录（命令直接退出）。--force-login 可强制走登录流程。
            already_logged_in = bool(provider.cfg.selectors.login_check)
            if (
                already_logged_in
                and sel is not None
                and not await provider.logged_out_visible(page)
                and not getattr(args, "force_login", False)
            ):
                print(f"[{name}] 已是登录状态（如需强制重新登录，加 --force-login）。")
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
                if not (provider.cfg.login.detect or provider.cfg.selectors.login_check):
                    print(
                        f"[{name}] 未配置 login.detect（登录标记）→ 不会自动检测；"
                        f"请完成登录后回终端按【回车】保存。"
                    )
                print(
                    f"[{name}] 请在弹出的浏览器窗口完成登录（Google / 验证码 / 滑块均可）。\n"
                    f"        完成后回到本终端按【回车】保存（也会尝试自动检测）。"
                    f"最长 {args.timeout:.0f}s，Ctrl+C 取消。"
                )
                # 登录指示器可能不准（如 ChatGPT）→ 支持用户手动回车确认
                confirm = None
                if sys.stdin.isatty():
                    confirm = asyncio.get_running_loop().run_in_executor(
                        None, sys.stdin.readline
                    )
                deadline = time.monotonic() + args.timeout
                ok = False
                while time.monotonic() < deadline:
                    if confirm is not None and confirm.done():
                        ok = True
                        print(f"[{name}] 已确认，保存登录态…")
                        break
                    # 只用"登录标记"判断，**绝不**用 input 兜底：
                    # 游客态站点（Gemini/豆包）输入框一直在，会把"没登录"误判成登录成功。
                    # 优先级：login.detect → selectors.login_check（真登录标记）；两者都空则不自动检测。
                    detect_sels = list(provider.cfg.login.detect) or list(
                        provider.cfg.selectors.login_check
                    )
                    if detect_sels and await extractor.first_match(page, detect_sels) is not None:
                        ok = True
                        print(f"[{name}] 自动检测到登录成功。")
                        break
                    await page.wait_for_timeout(1500)
                if not ok:
                    print(f"[{name}] 超时未确认（未保存）。")
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
                    info = compute_for_state_file(
                        out,
                        auth_cookies=provider.cfg.login.auth_cookies,
                        session_ttl_days=provider.cfg.login.session_ttl_days,
                        warn_days=(
                            provider.cfg.login.expiry_warn_days
                            or cfg.browser.auth_expiry_warn_days
                        ),
                        login_at=time.time(),
                    )
                    extra = f"，认证还剩 {info.days_left:.0f} 天" if info.days_left else ""
                    print(f"✅ [{name}] 已保存并导入 {url}（服务立即生效，无需重启{extra}）")
                    print(f"   文件：{out}")
                else:
                    print(
                        f"⚠️ [{name}] 已保存 {out}，但导入 {url} 失败：{msg}\n"
                        f"   稍后可手动导入：POST {url}/admin/{name}/login/state"
                        f"（body = state.json 内容）"
                    )
            return 0
        finally:
            await page.close()
    finally:
        await browser.stop()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "providers":
        return _cmd_providers(load_config(args.config))
    try:
        return asyncio.run(_login_flow(args))
    except KeyboardInterrupt:
        print("\n已取消（未保存/未导入）。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
