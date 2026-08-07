"""Provider 抽象基类：所有 Web AI 驱动的统一接口。"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import AsyncIterator, Literal

from playwright.async_api import Page

from ..browser.extractor import first_match
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
    """
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = str(m.get("content", ""))
        parts.append(f"<{role}>\n{content}\n</{role}>")
    return "\n\n".join(parts)


class BaseProvider(abc.ABC):
    """Web AI 驱动基类。

    子类只需实现 :meth:`generate`；登录检测、串行队列、非流式聚合
    均由基类提供。
    """

    name: str = ""

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

    async def check_login(self) -> bool:
        page = await self.browser.open_page(self.name)
        try:
            await page.goto(self.cfg.url, wait_until="domcontentloaded", timeout=30000)
            sel = await first_match(page, self.login_check_selectors)
            if sel is None:
                return False
            await page.wait_for_selector(sel, timeout=8000)
            return True
        except Exception:
            return False
        finally:
            await page.close()

    async def complete(self, messages: list[dict], model: str) -> tuple[str, str | None]:
        """非流式：返回 (content, reasoning_content)。"""
        chunks = [c async for c in self.generate(messages, model)]
        content = "".join(c.text for c in chunks if c.kind == "content")
        thinking = "".join(c.text for c in chunks if c.kind == "thinking")
        return content, thinking or None

    @abc.abstractmethod
    def generate(self, messages: list[dict], model: str) -> AsyncIterator[StreamChunk]:
        """打开新 Tab → 注入上下文 → 发送 → 增量产出响应。

        注意：实现必须是 async generator（含 yield），因此这里用普通 def 声明。
        """
        raise NotImplementedError

    # ---------- 内部工具 ----------

    async def _open_chat_page(self) -> Page:
        """打开聊天页并确认已登录；未登录抛 NotLoggedInError。"""
        page = await self.browser.open_page(self.name)
        await page.goto(self.cfg.url, wait_until="domcontentloaded", timeout=30000)
        try:
            sel = await first_match(page, self.login_check_selectors)
            if sel is None:
                raise TimeoutError("no login_check selector matched")
            await page.wait_for_selector(sel, timeout=10000)
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
