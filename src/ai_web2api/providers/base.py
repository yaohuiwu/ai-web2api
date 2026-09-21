"""Provider 抽象基类：所有 Web AI 驱动的统一接口。"""

from __future__ import annotations

import abc
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


class BaseProvider(abc.ABC):
    """Web AI 驱动基类。

    子类只需实现 :meth:`generate`；登录检测、串行队列、非流式聚合
    均由基类提供。
    """

    name: str = ""

    # thread 持久化用：从页面 URL 提取 provider 会话 id 的正则（如 DeepSeek
    # /a/chat/s/<uuid>）。None = 该 provider 不支持会话恢复（不落盘）。
    session_url_pattern: str | None = None

    def __init__(self, cfg: ProviderConfig, browser: BrowserManager):
        self.cfg = cfg
        self.name = cfg.name  # 覆盖类属性：同一驱动类可服务多个 provider 实例（context/登录态按名字隔离）
        self.browser = browser
        self.gate = SerialGate(cfg.queue.max_size, cfg.queue.timeout)

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

    async def check_login(self) -> bool:
        page = await self.browser.open_page(self.name, locale=self.locale)
        try:
            await page.goto(self.cfg.url, wait_until="domcontentloaded", timeout=30000)
            # 聊天页是 SPA，输入框在 domcontentloaded 后才渲染，需轮询等待
            sel = await wait_first_match(page, self.login_check_selectors, timeout=8.0)
            return sel is not None
        except Exception:
            return False
        finally:
            await page.close()

    def get_credentials(self) -> dict[str, str]:
        """从环境变量/.env 读取登录凭据（键名可配置，兼容 username/password 写法）。"""
        u = os.getenv(self.cfg.login.username_env) or os.getenv("username")
        p = os.getenv(self.cfg.login.password_env) or os.getenv("password")
        return {"username": (u or "").strip(), "password": (p or "").strip()}

    async def auto_login(self) -> dict:
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
            await page.goto(self.login_url, wait_until="domcontentloaded", timeout=30000)

            lp = cfg.login.page
            # SPA 在 domcontentloaded 后才渲染 DOM：等「已登录（聊天输入框）」
            # 或「登录表单（账号+密码输入框）」任一出现，再往下判断。
            appeared = await wait_first_match(
                page,
                [*self.login_check_selectors, *lp.username, *lp.password],
                timeout=15.0,
            )
            if appeared is None:
                return {
                    "ok": False,
                    "reason": "页面 15s 内未渲染出登录表单/聊天页（可能被风控/验证码页拦截），请手动登录",
                    "url": page.url,
                }

            # 已登录则幂等返回
            sel = await first_match(page, self.login_check_selectors)
            if sel is not None:
                await page.wait_for_timeout(800)
                return {"ok": True, "already_logged_in": True}

            # 若默认是验证码 tab，先切到"密码登录"
            tab_sel = await first_match(page, lp.password_tab)
            if tab_sel is not None:
                await page.locator(tab_sel).first.click()
                await page.wait_for_timeout(800)

            user_sel = await wait_first_match(page, lp.username, timeout=5.0)
            pwd_sel = await wait_first_match(page, lp.password, timeout=5.0)
            if user_sel is None or pwd_sel is None:
                return {
                    "ok": False,
                    "reason": f"登录页输入框未匹配（user={user_sel}, pwd={pwd_sel}）",
                    "url": page.url,
                }
            await page.locator(user_sel).first.fill(creds["username"])
            await page.locator(pwd_sel).first.fill(creds["password"])
            # React 受控输入：填完等一拍让状态提交（否则立刻点提交可能带上旧值），
            # 也能降低被风控判定为“机器快速填表”的概率。
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
                    return {"ok": True, "already_logged_in": False}
                await page.wait_for_timeout(1000)
            return {
                "ok": False,
                "reason": "登录超时（60s 内未进入聊天页，可能触发验证码/风控，请手动登录）",
                "url": page.url,
            }
        finally:
            await page.close()

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
    ) -> tuple[str, str | None]:
        """非流式：返回 (content, reasoning_content)。"""
        chunks = [c async for c in self.generate(
            messages, model,
            thread_mode=thread_mode, thread_page=thread_page,
            mode=mode, deep_think=deep_think, search=search,
            attachments=attachments,
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

        注意：实现必须是 async generator（含 yield），因此这里用普通 def 声明。
        """
        raise NotImplementedError

    # ---------- 内部工具 ----------

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
        await page.goto(self.cfg.url, wait_until="domcontentloaded", timeout=30000)
        try:
            # 聊天页是 SPA，输入框在 domcontentloaded 后才渲染，需轮询等待
            sel = await wait_first_match(page, self.login_check_selectors, timeout=15.0)
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
