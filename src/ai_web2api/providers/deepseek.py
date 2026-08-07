"""DeepSeek Web (chat.deepseek.com) 驱动。"""

from __future__ import annotations

import logging
import time
from typing import AsyncIterator, Literal

from playwright.async_api import Page

from ..browser import extractor
from ..core.errors import (
    ResponseTimeoutError,
    ThreadBusyError,
    ThreadExpiredError,
)
from .base import BaseProvider, StreamChunk, build_prompt, last_user_message

logger = logging.getLogger(__name__)


class DeepSeekProvider(BaseProvider):
    """chat.deepseek.com 驱动。"""

    name = "deepseek"
    # 会话 URL: chat.deepseek.com/a/chat/s/<uuid>（末段即 DeepSeek 会话 id，
    # 服务端保存用户会话不回收 → thread 持久化恢复可靠）
    session_url_pattern = r"/a/chat/s/([0-9a-fA-F-]{8,})"

    async def generate(
        self,
        messages: list[dict],
        model: str,
        *,
        thread_mode: str | None = None,
        thread_page=None,
    ) -> AsyncIterator[StreamChunk]:
        cfg = self.cfg
        resume = thread_mode == "resume"
        # resume 专属：页面可能仍在生成上一请求（DeepSeek 生成中输入=排队），
        # 发送后若迟迟无新容器 → 判定页面忙，抛 ThreadBusyError 销毁重建
        busy_timeout = cfg.thread_busy_timeout if resume else None
        page = thread_page or await self.open_chat_page()
        logger.info(
            "[%s] generate mode=%s url=%s", self.name, thread_mode or "stateless", page.url
        )
        try:
            input_sel = await extractor.first_match(page, cfg.selectors.input)
            if input_sel is None:
                if resume:
                    raise ThreadExpiredError(
                        f'provider "{self.name}" thread 页面失效（找不到输入框），会话已自动销毁',
                        provider=self.name,
                    )
                raise ResponseTimeoutError(
                    f'provider "{self.name}" 找不到输入框（input 选择器均未匹配）',
                    provider=self.name,
                )
            logger.info("[%s] input matched: %s", self.name, input_sel)

            # 发送前记录各候选容器的数量，之后只取"新增"的那个
            md_before = {s: await extractor.count_matches(page, s) for s in cfg.selectors.response_container}
            th_before = {s: await extractor.count_matches(page, s) for s in cfg.selectors.thinking_container}

            if resume:
                # 会话绑定续用：页面已有完整历史（页面为准），只发最后一条 user 消息
                prompt = last_user_message(messages)
            else:
                # 无状态 / create：完整历史拼单条 prompt 注入
                prompt = build_prompt(messages)
            logger.info("[%s] sending prompt (%d chars): %.60r", self.name, len(prompt), prompt)
            await self._send_prompt(page, input_sel, prompt)
            logger.info("[%s] prompt sent, polling", self.name)

            async for chunk in self._poll_response(page, md_before, th_before, busy_timeout):
                yield chunk
        finally:
            # 无状态请求：用完即关；thread 模式：页面留给 ThreadManager 管理
            if thread_mode is None and thread_page is None:
                await page.close()

    # ---------- 内部 ----------

    async def _send_prompt(self, page: Page, input_sel: str, prompt: str) -> None:
        cfg = self.cfg
        input_el = page.locator(input_sel).first
        logger.info("[%s] send: click input", self.name)
        await input_el.click()
        logger.info("[%s] send: fill", self.name)
        await input_el.fill(prompt)
        await page.wait_for_timeout(250)  # 等 UI 启用发送
        send_sel = await extractor.first_match(page, cfg.selectors.send_button)
        logger.info("[%s] send: button=%s", self.name, send_sel)
        if send_sel:
            await page.locator(send_sel).first.click()
        else:
            await page.keyboard.press("Enter")

    async def _poll_response(
        self,
        page: Page,
        md_before: dict[str, int],
        th_before: dict[str, int],
        busy_timeout: float | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """增量 diff 响应文本，产出 StreamChunk（thinking 与 content 分开）。

        容器选择器动态发现：先等任一"新增"标记出现（R1 思考区先出现、
        正文后出现），正文容器出现后即自动接管提取。
        """
        cfg = self.cfg
        stop_sels = cfg.selectors.stop_button
        timeout = cfg.response_timeout
        poll_ms = int(cfg.poll_interval * 1000)
        stable_polls = cfg.stable_polls
        min_wait = cfg.min_wait_before_stable

        # 阶段1：等待新回复标记出现（md 或 thinking 任一候选数量增加）
        md_sel: str | None = None
        th_sel: str | None = None
        start = time.monotonic()
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise ResponseTimeoutError(
                    f'provider "{self.name}" 发送后未检测到回复开始', provider=self.name
                )
            if busy_timeout is not None and elapsed > busy_timeout:
                # resume 且页面忙（在生成上一请求的内容，新消息只是排队）→ 立即失败
                raise ThreadBusyError(
                    f'provider "{self.name}" thread 页面正忙（上一请求未完成，发送被排队），'
                    f"会话已销毁，请稍后重试（thread_busy_timeout={busy_timeout:.0f}s）",
                    provider=self.name,
                )
            md_sel = await self._find_new(page, md_before)
            th_sel = await self._find_new(page, th_before)
            if md_sel or th_sel:
                break
            await page.wait_for_timeout(poll_ms)
        md_idx = md_before.get(md_sel) if md_sel else None
        th_idx = th_before.get(th_sel) if th_sel else None

        last = {"thinking": "", "content": ""}
        stable = 0
        stop_seen = False

        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise ResponseTimeoutError(
                    f'provider "{self.name}" 响应超时 ({timeout:.0f}s)', provider=self.name
                )

            # 动态发现（R1：正文容器在思考结束后才出现）
            if md_sel is None:
                found = await self._find_new(page, md_before)
                if found:
                    md_sel, md_idx = found, md_before[found]
            if th_sel is None:
                found = await self._find_new(page, th_before)
                if found:
                    th_sel, th_idx = found, th_before[found]

            md_text = await extractor.extract_markdown(page, md_sel, md_idx) if md_sel else ""
            th_text = await extractor.extract_markdown(page, th_sel, th_idx) if th_sel else ""

            changed = False
            pairs: list[tuple[Literal["thinking", "content"], str, str]] = [
                ("thinking", th_text, "thinking"),
                ("content", md_text, "content"),
            ]
            for kind, new, key in pairs:
                old = last[key]
                if new == old:
                    continue
                changed = True
                if new.startswith(old):
                    yield StreamChunk(kind, new[len(old):])
                # DOM 重渲染导致文本非前缀延续：丢弃本次增量，避免重复输出
                last[key] = new
            stable = 0 if changed else stable + 1

            # 结束判定
            done = False
            if stop_sels:
                vis = [await self._is_visible(page, s) for s in stop_sels]
                stop_visible = any(vis)
                if stop_visible:
                    stop_seen = True
                elif stop_seen:
                    done = True  # 出现过停止按钮且已消失 = 生成结束
            if not done and stable >= stable_polls and elapsed > min_wait:
                if last["thinking"] and not last["content"]:
                    # 思考已产出但正文未开始：可能是"思考→正文"的间隙，放宽判定
                    if stable >= stable_polls * 3:
                        done = True
                else:
                    done = True
            if done:
                return

            await page.wait_for_timeout(poll_ms)

    @staticmethod
    async def _find_new(page: Page, before: dict[str, int]) -> str | None:
        """返回第一个数量比发送前多的候选容器选择器。"""
        for s, n in before.items():
            if await extractor.count_matches(page, s) > n:
                return s
        return None

    @staticmethod
    async def _is_visible(page: Page, selector: str) -> bool:
        try:
            return await page.locator(selector).first.is_visible()
        except Exception:
            return False
