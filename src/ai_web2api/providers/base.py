"""Provider 抽象基类：所有 Web AI 驱动的统一接口。"""

from __future__ import annotations

import abc
import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import AsyncIterator, Literal

from playwright.async_api import Page

from ..browser.extractor import first_match, wait_first_match
from ..browser.manager import BrowserManager
from ..config import ProviderConfig
from ..core.errors import NotLoggedInError
from ..core.queue import SerialGate

logger = logging.getLogger(__name__)

# JS 注入脚本：textarea/input — 设值 + 派发事件触发 React 状态同步
_JS_TYPE_INPUT = """(el, text) => {
    el.value = text;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
}"""

# JS 注入脚本：contenteditable — innerText + <br> 换行 + InputEvent
_JS_TYPE_CONTENTEDITABLE = """(el, text) => {
    el.innerText = '';
    const lines = text.split('\\n');
    lines.forEach((line, i) => {
        if (i > 0) el.appendChild(document.createElement('br'));
        el.appendChild(document.createTextNode(line));
    });
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText' }));
}"""


@dataclass
class StreamChunk:
    """流式输出中的一个增量块。"""

    kind: Literal["thinking", "content"]
    text: str


def build_prompt(messages: list[dict]) -> str:
    """把 OpenAI messages 拼成单条上下文文本。

    无状态模式：每个请求是新会话，完整历史注入为一条 prompt，
    借用 Web 端的长上下文能力处理多轮对话。

    只拼用户会输入的内容（纯文本、换行分隔，不带 <role> 标签）——
    Web 输入框是用户打字的地方，尖括号内容可能被前端转义或干扰模型。
    """
    parts = [str(m.get("content", "")).strip() for m in messages]
    return "\n\n".join(p for p in parts if p)


def last_user_message(messages: list[dict]) -> str:
    """取最后一条 user 消息的文本（会话绑定续用：页面为准，只发这条）。"""
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", "")).strip()
    return ""


async def js_type(el, text: str) -> None:
    """JS 注入输入：瞬间完成，触发 React/ProseMirror 状态同步。

    对 textarea/input：设 value + dispatch input/change 事件。
    对 contenteditable：设 innerText（\n → <br>）+ dispatch InputEvent。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    is_content_editable = await el.evaluate("el => el.isContentEditable")
    if is_content_editable:
        await el.evaluate(_JS_TYPE_CONTENTEDITABLE, text)
    else:
        await el.evaluate(_JS_TYPE_INPUT, text)


class BaseProvider(abc.ABC):
    """Web AI 驱动基类。

    子类只需实现 :meth:`generate`；登录检测、串行队列、非流式聚合
    均由基类提供。
    """

    name: str = ""

    # thread 持久化用：从页面 URL 提取 provider 会话 id 的正则（如 DeepSeek
    # /a/chat/s/<uuid>）。None = 该 provider 不支持会话恢复（不落盘）。
    session_url_pattern: str | None = None

    # thread 恢复时等"输入框就绪"的上限（秒）：SPA 渲染慢的站点（ChatGPT 实测 >2s）需要留足；
    # 这只是**超时上限**（每 0.3s 轮询，命中即返回），不是固定等待。超时后 reload 再等一半时长。
    restore_timeout: float = 20.0

    # 自动登录重试之间的等待（秒）；测试可置 0
    LOGIN_RETRY_DELAY: float = 1.5

    def __init__(self, cfg: ProviderConfig, browser: BrowserManager):
        self.cfg = cfg
        self.name = cfg.name  # 覆盖类属性：同一驱动类可服务多个 provider 实例（context/登录态按名字隔离）
        self.browser = browser
        self.gate = SerialGate(
            cfg.queue.max_size, cfg.queue.timeout, max_inflight=cfg.queue.max_inflight
        )
        # 本轮捕获的交互组件（iframe widget）元数据；每轮发送前清空，路由 take_widgets() 取走
        self._widgets: list[dict] = []
        self._widget_store_obj = None
        # 指标落盘（外部注入，None = 只记内存时间线）
        self.metrics_store: object = None

    # ---------- 对外接口 ----------

    @property
    def exposed_models(self) -> list[str]:
        return [m.name for m in self.cfg.models]

    @property
    def login_check_selectors(self) -> list[str]:
        return self.cfg.selectors.login_check or self.cfg.selectors.input

    @property
    def locale(self) -> str:
        """页面语言（provider 可覆盖；空则用全局 browser.locale）。"""
        return self.cfg.locale or self.browser.default_locale

    @property
    def login_url(self) -> str:
        """登录页 URL（provider 可单独指定，如 Qwen /auth；空则用聊天页 URL）。"""
        return self.cfg.login.url or self.cfg.url

    def session_url(self, url_id: str) -> str | None:
        """thread 恢复 URL：由 ``provider.session_url`` 模板生成（未配返回 None）。"""
        tpl = self.cfg.session_url
        if not tpl:
            return None
        return tpl.format(base=self.cfg.url.rstrip("/"), id=url_id)

    async def logged_out_visible(self, page: "Page") -> bool:
        """是否可见「未登录标记」（``selectors.logged_out``）—— 反向判定。

        典型场景：豆包游客态也有输入框，``login_check`` 回退到 input 会误判已登录；
        配上 ``logged_out: [".login-button"]`` 即可正确判定。
        """
        for sel in self.cfg.selectors.logged_out:
            try:
                if await page.locator(sel).first.is_visible():
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def check_login(self) -> bool | None:
        """检查登录态。

        True=已登录；False=确实未登录；None=**不确定**（页面还没 boot/网络异常），
        调用方不应据此判定未登录（否则 Qwen 这类站点会在加载屏阶段误报）。
        """
        page = await self.browser.open_page(self.name, locale=self.locale)
        try:
            sel = await self._goto_ready(
                page, self.cfg.url, self.login_check_selectors, total_timeout=35.0
            )
            if await self.logged_out_visible(page):
                logger.info(
                    "check_login(%s): 命中未登录标记 %s → 判定未登录",
                    self.name, self.cfg.selectors.logged_out,
                )
                return False
            if sel is not None:
                return True
            if await self._page_stuck_loading(page):
                logger.info(
                    "check_login(%s): 页面仍在加载（loading 遮罩/空 body），结果不确定", self.name
                )
                return None
            return False
        except Exception as e:  # noqa: BLE001
            logger.info("check_login(%s): 页面异常（%s），结果不确定", self.name, e)
            return None
        finally:
            await page.close()

    async def _page_stuck_loading(self, page: "Page") -> bool:
        """页面是否还没渲染出内容（loading 遮罩可见 / body 基本为空）。"""
        for s in self.cfg.login.page.splash:
            try:
                if await page.locator(s).first.is_visible():
                    return True
            except Exception:
                pass
        try:
            txt = (await page.inner_text("body") or "").strip()
        except Exception:
            return True
        return not txt

    async def _goto_ready(
        self,
        page: "Page",
        url: str,
        selectors: list[str],
        *,
        total_timeout: float = 35.0,
    ) -> str | None:
        """打开 ``url`` 并等任一选择器就绪；若卡在加载屏则 reload 再等（限总时长）。

        站点（如 Qwen）被限流时 SPA 会卡在启动屏，直接判定“未登录”会误报；
        这里 reload 一次并给足时间。返回命中的选择器或 None。
        """
        deadline = time.monotonic() + total_timeout
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:  # noqa: BLE001
            logger.info("[%s] goto %s 失败：%s", self.name, url, e)
            return None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sel = await wait_first_match(page, selectors, timeout=min(15.0, remaining))
            if sel is not None:
                return sel
            if await self._page_stuck_loading(page) and time.monotonic() < deadline - 8:
                logger.info("[%s] 页面卡在加载屏，reload 重试", self.name)
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:  # noqa: BLE001
                    return None
                continue
            return None

    def get_credentials(self) -> dict[str, str]:
        """从环境变量/.env 读取登录凭据（键名可配置，兼容 username/password 写法）。"""
        u = os.getenv(self.cfg.login.username_env) or os.getenv("username")
        p = os.getenv(self.cfg.login.password_env) or os.getenv("password")
        return {"username": (u or "").strip(), "password": (p or "").strip()}

    async def auto_login(self) -> dict:
        """自动登录（带重试）。

        网页登录首发提交可能静默无效（React 受控输入 / 风险校验 / 重渲染）——重试通常即成功。
        重试次数取 ``login.retries``（默认 3）。成功且发生重试时附 ``attempts``。
        """
        attempts = max(1, int(getattr(self.cfg.login, "retries", 3) or 1))
        result: dict = {}
        for i in range(attempts):
            result = await self._auto_login_once()
            if result.get("ok"):
                if i:
                    result["attempts"] = i + 1
                return result
            if i + 1 < attempts:
                logger.info(
                    "[%s] 自动登录第 %d/%d 次失败：%s；重试…",
                    self.name, i + 1, attempts, result.get("reason", ""),
                )
                await asyncio.sleep(self.LOGIN_RETRY_DELAY)
        return result

    async def _auto_login_once(self) -> dict:
        """用 .env 中的账号密码自动登录（login.mode=auto）。

        流程：打开登录页 → （必要时切到密码 tab）→ 填账号/密码 → 提交 →
        等聊天页出现 → storage_state 落盘。已登录时幂等返回。
        返回 {"ok": bool, "reason"?: str, "already_logged_in"?: bool}。
        """
        cfg = self.cfg
        creds = self.get_credentials()
        if not creds["username"] or not creds["password"]:
            return {
                "ok": False,
                "reason": (
                    f'.env 未配置凭据键名 {cfg.login.username_env} / '
                    f"{cfg.login.password_env}（或 username / password）"
                ),
            }
        page = await self.browser.open_page(self.name, locale=self.locale)
        try:
            lp = cfg.login.page
            # SPA 在 domcontentloaded 后才渲染：等「已登录（聊天输入框）」或「登录表单」
            # （卡启动屏会 reload 重试，避免 Qwen 限流时误判）
            appeared = await self._goto_ready(
                page,
                self.login_url,
                [*self.login_check_selectors, *lp.username, *lp.password],
                total_timeout=35.0,
            )
            if appeared is None:
                return await self._login_fail(
                    page, "页面 35s 内未渲染出登录表单/聊天页（可能被风控/验证码页拦截），请手动登录"
                )

            # 已登录则幂等返回。用 wait：登录态下 /auth 会重定向到聊天页，
            # appeared 可能先匹配到登录表单，等重定向完成再确认聊天页。
            sel = await wait_first_match(page, self.login_check_selectors, timeout=8.0)
            if sel is not None:
                await page.wait_for_timeout(800)
                self.browser.clear_login_error(self.name)
                return {"ok": True, "already_logged_in": True}

            # 等加载遮罩消失：否则 #splash-screen 会拦截后续点击（Qwen 实测）
            await self._wait_loading_gone(page, lp)

            # 默认可能是“验证码登录”tab → 切到“密码登录”（Qwen 实测）：
            # 点一次后等密码框；没出来就再点一次（点击可能被重渲染吞掉）。
            for _ in range(2):
                if await first_match(page, lp.password) is not None:
                    break
                tab_sel = await first_match(page, lp.password_tab)
                if tab_sel is None:
                    break
                try:
                    await page.locator(tab_sel).first.click()
                except Exception:  # noqa: BLE001
                    pass
                await wait_first_match(page, lp.password, timeout=6.0)

            user_sel = await wait_first_match(page, lp.username, timeout=8.0)
            pwd_sel = await wait_first_match(page, lp.password, timeout=8.0)
            if user_sel is None or pwd_sel is None:
                return await self._login_fail(
                    page, f"登录页输入框未匹配（user={user_sel}, pwd={pwd_sel}）"
                )
            # JS 注入输入（而非 fill/press_sequentially）：React 受控组件
            # fill() 只改 DOM 值、state 仍为空 → 静默跳过。js_type 设值 +
            # 派发事件可正确触发 React 状态同步，且瞬间完成。
            async def _type(el, text: str) -> None:
                await el.click()
                await js_type(await el.element_handle(), text)

            await _type(page.locator(user_sel).first, creds["username"])
            await _type(page.locator(pwd_sel).first, creds["password"])
            await page.wait_for_timeout(800)

            submit_sel = await first_match(page, lp.submit)
            if submit_sel is not None:
                await page.locator(submit_sel).first.click()
            else:
                await page.keyboard.press("Enter")

            # 等待跳转聊天页（= 登录成功）
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if await first_match(page, self.login_check_selectors) is not None:
                    # 刚登录成功 → 登录态已变，必须落盘（save_state 的"按需"规则之外）
                    self.browser.mark_state_dirty(self.name)
                    await self.browser.save_state(self.name)
                    self.browser.clear_login_error(self.name)
                    return {"ok": True, "already_logged_in": False}
                await page.wait_for_timeout(1000)
            return await self._login_fail(
                page, "登录超时（60s 内未进入聊天页，可能触发验证码/风控，请手动登录）"
            )
        finally:
            await page.close()

    async def _login_fail(self, page: "Page", reason: str) -> dict:
        """登录失败：先抓一张当前页面截图（webui 可展示）再返回失败结果。"""
        path = await self.browser.save_login_error(self.name, page)
        out: dict = {"ok": False, "reason": reason, "url": page.url}
        if path:
            out["screenshot"] = f"/admin/{self.name}/login/screenshot"
        return out

    @staticmethod
    async def _wait_loading_gone(page: "Page", lp) -> None:
        """等登录页加载遮罩消失（state=hidden：不存在也算通过）。"""
        for sel in lp.splash:
            try:
                await page.wait_for_selector(sel, state="hidden", timeout=15000)
            except Exception:  # noqa: BLE001
                pass

    async def complete(
        self,
        messages: list[dict],
        model: str,
        *,
        thread_mode: str | None = None,
        thread_page=None,
        mode: str | None = None,
        deep_think: bool | None = None,
        search: bool | None = None,
        attachments: list[dict] | None = None,
        options: dict | None = None,
        prompt_override: str | None = None,
    ) -> tuple[str, str | None]:
        """非流式：返回 (content, reasoning_content)。"""
        chunks = [c async for c in self.generate(
            messages, model,
            thread_mode=thread_mode, thread_page=thread_page,
            mode=mode, deep_think=deep_think, search=search,
            attachments=attachments, options=options,
            prompt_override=prompt_override,
        )]
        content = "".join(c.text for c in chunks if c.kind == "content")
        thinking = "".join(c.text for c in chunks if c.kind == "thinking")
        return content, thinking or None

    @abc.abstractmethod
    def generate(
        self,
        messages: list[dict],
        model: str,
        *,
        thread_mode: str | None = None,
        thread_page=None,
        mode: str | None = None,
        deep_think: bool | None = None,
        search: bool | None = None,
        attachments: list[dict] | None = None,
        options: dict | None = None,
        prompt_override: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """打开新 Tab → 注入上下文 → 发送 → 增量产出响应。

        thread_mode:
          - None（默认）：无状态——自开新页，注入完整历史，用完关闭页面；
          - "create"：页面由 ThreadManager 打开（thread_page 传入），
            注入完整历史（支持从无状态迁移），页面留给 manager 复用；
          - "resume"：复用 thread_page（页面已有完整历史），只发最后一条
            user 消息，不注入历史、不点新对话。

        mode/deep_think/search：Web 端选项（通用值，provider 映射自己的 UI）。
        mode 由 provider 决定语义：有模式区的 UI → 仅新会话可点 radio，resume 忽略；
        无模式区的 UI（新版 DeepSeek 三模式合一）→ 翻译成开关组合，每次请求可生效。
        deep_think/search 每次请求生效；页面无对应开关时忽略。
        options：provider 自定义选项透传（不经 schema，如 Qwen 的 web_search），
        默认忽略，驱动可在 `_apply_extra_options` 里消费。
        prompt_override：**直接指定要发送的文本**（Function Calling 在协议层拼好 prompt 后传入），
        非空时驱动不再自行拼装（跳过 build_prompt / last_user_message）。

        注意：实现必须是 async generator（含 yield），因此这里用普通 def 声明。
        """
        raise NotImplementedError

    # ---------- 内部工具 ----------

    def note_queued(self, waited_ms: float) -> None:
        """等闸门的时长（毫秒）——子类可记进请求时间线；默认忽略。"""
        return None

    def init_scripts(self) -> list[str]:
        """页面级注入脚本（goto 前生效）。子类可覆写（如 DeepSeek 的网络监听）。"""
        return []

    async def open_chat_page(self) -> Page:
        """打开聊天页并确认已登录；未登录抛 NotLoggedInError。

        供无状态请求与 ThreadManager（会话绑定）共用。
        """
        page = await self.browser.open_page(
            self.name, init_scripts=self.init_scripts(), locale=self.locale
        )
        try:
            # 卡启动屏时 _goto_ready 会 reload 重试（Qwen 限流常见），避免误报未登录
            sel = await self._goto_ready(
                page, self.cfg.url, self.login_check_selectors, total_timeout=40.0
            )
            if sel is None:
                raise TimeoutError("no login_check selector matched")
        except Exception:
            await page.close()
            raise NotLoggedInError(
                f'provider "{self.name}" 未登录，请先运行: POST /admin/{self.name}/login/start',
                provider=self.name,
            ) from None
        # 可选：点"新建对话"隔离会话
        for sel in self.cfg.selectors.new_chat_button:
            try:
                if await first_match(page, [sel]):
                    await page.locator(sel).first.click(timeout=3000)
                    await page.wait_for_timeout(500)
                    break
            except Exception:
                logger.debug("new_chat_button click failed, continue")
        return page
