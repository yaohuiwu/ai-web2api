"""DeepSeek Web (chat.deepseek.com) 驱动。"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from typing import AsyncIterator, Literal

from playwright.async_api import Page

from ..browser import extractor
from ..core.errors import (
    AttachmentError,
    ProviderError,
    ResponseTimeoutError,
    ThreadBusyError,
    ThreadExpiredError,
    UnsupportedModeError,
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
        mode: str | None = None,
        deep_think: bool | None = None,
        search: bool | None = None,
        attachments: list[dict] | None = None,
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

            # 发送前应用模式/开关（在记录容器数量之前，避免 UI 重渲染影响增量判定）
            await self._apply_options(page, mode, deep_think, search, can_set_mode=not resume)

            # 发送前上传附件（图片等）：写入输入框，发送时随消息带上
            await self._upload_attachments(page, attachments)

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

    async def _apply_options(
        self,
        page: Page,
        mode: str | None,
        deep_think: bool | None,
        search: bool | None,
        can_set_mode: bool,
    ) -> None:
        """发送前把请求参数映射到页面 UI（模式 radio / 开关 toggle）。

        - mode：仅新会话（create/无状态）可设；resume 时页面在会话页，无模式区，忽略。
        - deep_think/search：每次请求按参数设置（已是目标态则跳过）；页面无对应开关时忽略。
        """
        cfg = self.cfg
        sels = cfg.selectors
        if mode:
            if not can_set_mode:
                # resume：会话页无模式区（DeepSeek 实测），忽略并提示
                logger.info("[%s] mode=%s ignored (resume: 会话页不可切换模式)", self.name, mode)
            else:
                cands = sels.mode_button.get(mode, [])
                if not cands:
                    raise UnsupportedModeError(
                        f'provider "{self.name}" 不支持 mode="{mode}"（可选: {list(sels.mode_button) or "无"}）',
                        provider=self.name,
                    )
                sel = await extractor.first_match(page, cands)
                if sel is None:
                    raise UnsupportedModeError(
                        f'provider "{self.name}" mode="{mode}" 的按钮未在页面找到'
                        f"（候选: {cands}）",
                        provider=self.name,
                    )
                if not await self._is_checked(page, sel, sels.mode_checked):
                    logger.info("[%s] mode: click %s -> %s", self.name, sel, mode)
                    await page.locator(sel).first.click()
                    await page.wait_for_timeout(1200)  # 等模式切换生效（可能重渲染）
                else:
                    logger.info("[%s] mode=%s already active", self.name, mode)
        if deep_think is not None:
            await self._set_toggle(page, "deep_think", sels.toggle_button.get("deep_think", []), deep_think)
        if search is not None:
            await self._set_toggle(page, "search", sels.toggle_button.get("search", []), search)

    async def _set_toggle(self, page: Page, field: str, cands: list[str], want_on: bool) -> None:
        """设置开关到目标状态（已是目标态则跳过）；UI 无此开关时忽略（如被模式隐藏）。"""
        if not cands:
            return
        sel = await extractor.first_match(page, cands)
        if sel is None:
            logger.info("[%s] toggle %s=%s skipped: 页面无此开关（可能被当前模式隐藏）", self.name, field, want_on)
            return
        is_on = await self._is_checked(page, sel, self.cfg.selectors.toggle_checked)
        if is_on == want_on:
            logger.info("[%s] toggle %s already %s", self.name, field, "on" if want_on else "off")
            return
        logger.info("[%s] toggle %s -> %s", self.name, field, "on" if want_on else "off")
        await page.locator(sel).first.click()
        await page.wait_for_timeout(800)  # 等 toggle 状态渲染

    MAX_ATTACHMENTS = 50   # DeepSeek tooltip：最多 50 个
    MAX_ATTACH_BYTES = 100 * 1024 * 1024  # 每个最大 100MB

    async def _upload_attachments(self, page: Page, attachments: list[dict] | None) -> None:
        """发送前把请求附件上传到输入框（DeepSeek：仅快速/识图模式支持，仅识别图片文字）。

        - data URL → base64 解码写临时文件；http(s) URL → 下载后上传
        - 上传入口：selectors.upload_input（默认 input[type=file]），找不到 → 400
        - 上传完成后等待预览出现（输入框上方的 blob 图片），再返回
        """
        if not attachments:
            return
        if len(attachments) > self.MAX_ATTACHMENTS:
            raise AttachmentError(
                f"附件数量 {len(attachments)} 超过上限 {self.MAX_ATTACHMENTS} 个", provider=self.name
            )
        sels = self.cfg.selectors.upload_input or ["input[type=file]"]
        sel = await extractor.first_match(page, sels)
        if sel is None:
            raise AttachmentError(
                'provider "%s" 当前页面/模式不支持附件上传（未找到上传入口 input[type=file]），'
                "DeepSeek 仅快速/识图模式支持" % self.name,
                provider=self.name,
            )
        paths: list[str] = []
        tmpdir: str | None = None
        try:
            import uuid
            for att in attachments:
                name = att.get("name", "image")
                data = att.get("data")
                if data:
                    import base64
                    try:
                        raw = base64.b64decode(data)
                    except Exception:
                        raise AttachmentError(f"附件 {name} base64 解码失败", provider=self.name)
                elif att.get("url"):
                    raw = await asyncio.to_thread(self._download_attachment, att["url"])
                else:
                    raise AttachmentError(f"附件 {name} 缺少 data/url", provider=self.name)
                if len(raw) > self.MAX_ATTACH_BYTES:
                    raise AttachmentError(
                        f"附件 {name} 超过上限 100MB", provider=self.name
                    )
                if tmpdir is None:
                    tmpdir = os.path.join(tempfile.gettempdir(), f"aiw2a_{uuid.uuid4().hex[:8]}")
                    os.makedirs(tmpdir, exist_ok=True)
                safe_name = os.path.basename(name) or "image"  # 防路径穿越；保留原名（上传后 chip 显示原名）
                path = os.path.join(tmpdir, safe_name)
                with open(path, "wb") as f:
                    f.write(raw)
                paths.append(path)
            logger.info("[%s] upload %d attachment(s) via %s", self.name, len(paths), sel)
            await page.locator(sel).first.set_input_files(paths)
            # 等上传完成：输入框上方的 blob 图片预览出现
            # （排除 .ds-message 内的历史消息图片；位置范围 500px 兼容布局差异）
            ta = page.locator(self.cfg.selectors.input[0] if self.cfg.selectors.input else "textarea").first
            try:
                box = await ta.bounding_box()
            except Exception:
                box = None
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    cnt = await page.evaluate(
                        """(ty) => {
                          const imgs = document.querySelectorAll("img[src^='blob:']");
                          let n = 0;
                          for (const im of imgs) {
                            if (im.closest('.ds-message')) continue;  // 消息区图片不算
                            const r = im.getBoundingClientRect();
                            if (r.width > 10 && r.height > 10 && ty !== null &&
                                r.y < ty && r.y > ty - 500) n++;
                          }
                          return n;
                        }""",
                        box["y"] if box else None,
                    )
                    if cnt >= len(paths):
                        break
                except Exception:
                    pass
                await page.wait_for_timeout(500)
            await page.wait_for_timeout(800)  # 等上传状态稳定
        finally:
            import shutil
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _download_attachment(url: str, timeout: float = 30) -> bytes:
        """同步下载 http(s) 附件（asyncio.to_thread 包裹）。"""
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    @staticmethod
    async def _is_checked(page: Page, sel: str, checked_sels: list[str]) -> bool:
        """用 checked 选择器判断元素当前是否选中/开启（el.matches 检查）。"""
        checked_sel = None
        for s in checked_sels:
            try:
                if await page.locator(s).first.count() > 0:
                    checked_sel = s
                    break
            except Exception:
                continue
        if checked_sel is None:
            return False
        try:
            return bool(await page.locator(sel).first.evaluate(
                "(el, s) => el.matches(s)", checked_sel
            ))
        except Exception:
            return False

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
