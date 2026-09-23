"""通用网页聊天引擎 ``WebChatProvider``。

把「开页 → 应用选项 → 上传附件 → 发送 → 网络优先/DOM 兜底轮询 → diff 增量 →
忙/失效/超时判定」这套与站点无关的流程从具体驱动里抽出来。子类只实现少量钩子：

- ``init_scripts()``：页面级注入（如 XHR/SSE 网络抓取）；默认无 → 直接走 DOM 轮询。
- ``_parse_sse_snapshot(text)``：网络快照 → (thinking, content)；默认抛
  ``_NetFallback`` → 自动降级 DOM。
- ``_extract_thinking(page, sel, idx)``：思考区文本；默认取元素 innerText。
- ``MODE_PRESETS``：``mode`` → 开关组合（无模式区的 UI 用）。
- ``session_url(url_id)``：thread 恢复 URL（默认用 ``cfg.session_url`` 模板）。
- 常量 ``MAX_ATTACHMENTS`` / ``ATTACH_PREVIEW_EXCLUDE`` 等：附件行为微调。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from typing import AsyncIterator, Literal

from playwright.async_api import Page

from ..browser import extractor
from ..core.errors import (
    AttachmentError,
    ResponseTimeoutError,
    ThreadBusyError,
    ThreadExpiredError,
    UnsupportedModeError,
)
from .base import BaseProvider, StreamChunk, build_prompt, last_user_message

logger = logging.getLogger(__name__)


def _pending_increment(sent: str, new: str) -> str:
    """从"已发出的文本"到"当前文本"的增量；**只在纯前缀追加时给出**。

    非前缀（DOM 重渲染改写）或文本变短 → 返回空，宁可少发也不发脏数据；
    缺失的部分由定稿时的 :func:`_suffix_after` 兜底补上。
    """
    if not new or new == sent:
        return ""
    if new.startswith(sent):
        return new[len(sent):]
    return ""


def _suffix_after(sent: str, final: str) -> str:
    """定稿时：返回 ``final`` 中**尚未发出**的部分（按最长公共前缀算）。

    预览流正常工作时等价于"纯追加的尾巴"；若中途发生过重渲染改写，
    则返回从分歧点开始的全部内容（尽量保证不丢字；协议不支持"撤回重发"）。
    """
    if not sent:
        return final
    if final.startswith(sent):
        return final[len(sent):]
    n = 0
    for a, b in zip(sent, final):
        if a != b:
            break
        n += 1
    logger.debug("预览流与定稿文本在前 %d 字后分歧（重渲染），按分歧点补发尾部", n)
    return final[n:]


def _diff_increment(old: str, new: str) -> str:
    """计算增量文本：从 old 到 new 应产出的新内容。

    处理 markdown 渐进渲染导致的"包裹"重渲染：先渲染纯文本，随后 DOM 重渲染为
    代码块/表格/加粗（如 `print("hello")` → ` ```python\\nprint("hello")\\n``` `），
    旧文本不再是新文本的前缀。
    - old 是 new 的子串（内容被包裹/中间插入）→ 从 old 之后继续产出，不丢内容
    - new 以 old 开头（普通打字机追加）→ 产出尾部增量
    - 其余（旧文本被改写，非子串非前缀）→ 返回空，丢弃本次增量避免重复
    """
    if old and old in new:
        return new[new.index(old) + len(old):]
    if new.startswith(old):
        return new[len(old):]
    return ""


class _NetFallback(Exception):
    """网络监听通道不可用/失效 → 调用方降级到 DOM 轮询。"""


class WebChatProvider(BaseProvider):
    """站点无关的网页聊天引擎。"""

    # 网络抓取通道在页面里用的全局量名（init_scripts 注入的 JS 写入）
    NET_GLOBAL = "__aiw2a_sse"
    NET_GLOBAL_DONE = "__aiw2a_sse_done"

    # mode → 开关组合（页面无模式选择区时的翻译表；子类按需覆盖）
    MODE_PRESETS: dict[str, dict[str, bool]] = {}

    # 附件：数量/大小上限（None = 不限），预览等待与判重排除
    MAX_ATTACHMENTS: int | None = None
    MAX_ATTACH_BYTES: int = 100 * 1024 * 1024
    ATTACH_WAIT_SECONDS: float = 60.0
    ATTACH_INPUT_TIMEOUT: float = 3.0  # 找文件 input 的等待上限（秒）
    ATTACH_PREVIEW_EXCLUDE: str = ""  # 预览计数时排除的容器（如消息区）

    # 下拉菜单 option 出现的等待上限（秒）
    MENU_OPTION_TIMEOUT: float = 5.0


    # ---------- 主流程 ----------

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
        options: dict | None = None,
        prompt_override: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        cfg = self.cfg
        resume = thread_mode == "resume"
        # resume 专属：页面可能仍在生成上一请求（生成中输入=排队），
        # 发送后若迟迟无新容器 → 判定页面忙，抛 ThreadBusyError 销毁重建
        busy_timeout = cfg.thread_busy_timeout if resume else None
        page = thread_page or await self.open_chat_page()
        logger.info(
            "[%s] generate mode=%s url=%s", self.name, thread_mode or "stateless", page.url
        )
        try:
            input_sel = await self._ensure_input(page, resume)
            logger.info("[%s] input matched: %s", self.name, input_sel)

            # 发送前应用模型/模式/开关（在记录容器数量之前，避免 UI 重渲染影响增量判定）
            await self._apply_model(page, model, can_set=not resume)
            await self._apply_options(
                page, mode, deep_think, search, can_set_mode=not resume, options=options
            )

            # 发送前上传附件（图片等）：写入输入框，发送时随消息带上
            await self._upload_attachments(page, attachments)

            # 发送前记录各候选容器的数量，之后只取"新增"的那个
            md_before = {
                s: await extractor.count_matches(page, s)
                for s in cfg.selectors.response_container
            }
            th_before = {
                s: await extractor.count_matches(page, s)
                for s in cfg.selectors.thinking_container
            }

            if prompt_override is not None:
                # Function Calling：协议层已拼好 prompt，直发
                prompt = prompt_override
            elif resume:
                # 会话绑定续用：页面已有完整历史（页面为准），只发最后一条 user 消息
                prompt = last_user_message(messages)
            else:
                # 无状态 / create：完整历史拼单条 prompt 注入
                prompt = build_prompt(messages)
            logger.info("[%s] sending prompt (%d chars): %.60r", self.name, len(prompt), prompt)
            if self.net_enabled:
                await self._reset_net_capture(page)
            if self.net_observe:
                self._observe_net(page)          # 观测型旁听（状态码/时序）
            await self._dismiss_overlays(page, reason="发送前")   # 弹窗会挡住输入/发送
            try:
                await self._send_prompt(page, input_sel, prompt)
            except ResponseTimeoutError:
                raise
            except Exception as exc:  # noqa: BLE001  遮罩拦截/元素不可交互 → 明确报错，别 500
                raise ResponseTimeoutError(
                    f'provider "{self.name}" 发送失败：{exc}（可能被弹窗/遮罩拦截，已尝试关闭；请重试）',
                    provider=self.name,
                ) from exc
            # 确认网页端真的接受了这条消息：点发送成功 ≠ 消息发出（实测 ChatGPT 会静默丢掉），
            # 否则消息在页面上"消失"、客户端只等到超时 → 用户表现为"丢了一个响应"。
            if not await self._confirm_sent(page, input_sel, prompt):
                logger.warning("[%s] 发送后输入框未清空 → 判定未发出，重试一次", self.name)
                await self._dismiss_overlays(page, reason="发送未生效")  # 常见原因：弹窗挡住发送
                await self._send_prompt(page, input_sel, prompt)
                if not await self._confirm_sent(page, input_sel, prompt):
                    raise ResponseTimeoutError(
                        f'provider "{self.name}" 消息未能发出：网页端没有接受这条输入'
                        f"（输入框仍保留原文，常见于账号限流/风控或页面未就绪），请稍后重试",
                        provider=self.name,
                    )
            logger.info("[%s] prompt sent, polling", self.name)

            self._widgets = []                                    # 每轮重置组件捕获
            frames_before = {f.url for f in page.frames if f.url}
            async for chunk in self._poll_response(
                page, md_before, th_before, busy_timeout,
                input_sel=input_sel, prompt=prompt, frames_before=frames_before,
            ):
                yield chunk
        finally:
            self._stop_observe(page)             # 摘掉旁听监听，避免跨请求累积
            # 无状态请求：用完即关；thread 模式：页面留给 ThreadManager 管理
            if thread_mode is None and thread_page is None:
                await page.close()

    async def _ensure_input(self, page: Page, resume: bool) -> str:
        """定位输入框；resume 时页面被打断先 reload 恢复，仍失败判定失效。

        用 wait_first_match（而非 instant）：SPA 导航/重渲染/加载遮罩期间输入框会短暂消失，
        立刻判定会误报“页面失效”。
        """
        cfg = self.cfg
        input_sel = await extractor.wait_first_match(page, cfg.selectors.input, timeout=6.0)
        if input_sel is None and resume:
            # 页面可能正被 SPA 导航/后台重载打断（count() 瞬时异常被吞成 0）。
            # 先 reload 当前会话页恢复，仍失败才判定失效销毁。
            logger.info("[%s] resume page input not matched, reloading to recover", self.name)
            try:
                await page.reload(wait_until="domcontentloaded", timeout=30000)
                input_sel = await extractor.wait_first_match(
                    page, cfg.selectors.input, timeout=12.0
                )
            except Exception:
                input_sel = None
            if input_sel is not None:
                logger.info("[%s] recovered after reload: %s", self.name, input_sel)
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
        return input_sel

    # ---------- 选项（模式 / 开关 / 模型） ----------

    async def _apply_options(
        self,
        page: Page,
        mode: str | None,
        deep_think: bool | None,
        search: bool | None,
        can_set_mode: bool,
        options: dict | None = None,
    ) -> None:
        """发送前把请求参数映射到页面 UI。

        - 页面有模式区（``selectors.mode_button`` 非空）：mode → 点 radio；仅新会话可设。
        - 页面无模式区：mode → ``MODE_PRESETS`` 翻译成开关组合，显式参数优先。
        - ``deep_think`` / ``search``：每次请求按参数设开关，页面无对应开关时忽略。
        - 模型选择：见 :meth:`_apply_model`（默认无操作，子类/配置启用）。
        - ``options``：provider 自定义选项，交给 :meth:`_apply_extra_options`。
        """
        cfg = self.cfg
        sels = cfg.selectors
        if mode:
            if sels.mode_menu.trigger and sels.mode_menu.option:
                # 下拉菜单形态：在输入框内，resume 时也可切
                label = sels.mode_menu.labels.get(mode)
                if label is None:
                    logger.warning(
                        "[%s] mode=%r 无 labels 映射（%s），忽略",
                        self.name, mode, list(sels.mode_menu.labels),
                    )
                else:
                    await self._select_menu_option(page, sels.mode_menu, label, what="mode")
            elif not sels.mode_button:
                # 无模式选择区：mode 只能翻译成开关
                preset = self.MODE_PRESETS.get(mode)
                if preset is None:
                    logger.warning(
                        "[%s] mode=%r 未知（页面无模式区），忽略；可用预设: %s",
                        self.name, mode, list(self.MODE_PRESETS),
                    )
                else:
                    if deep_think is None:
                        deep_think = preset["deep_think"]
                    if search is None:
                        search = preset["search"]
                    logger.info(
                        "[%s] mode=%s → 开关预设 %s（显式参数优先）",
                        self.name, mode, preset,
                    )
            elif not can_set_mode:
                logger.info("[%s] mode=%s ignored (resume: 会话页不可切换模式)", self.name, mode)
            else:
                cands = sels.mode_button.get(mode, [])
                if not cands:
                    raise UnsupportedModeError(
                        f'provider "{self.name}" 不支持 mode="{mode}"'
                        f"（可选: {list(sels.mode_button) or '无'}）",
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
            await self._set_toggle(
                page, "deep_think", sels.toggle_button.get("deep_think", []), deep_think
            )
        if search is not None:
            await self._set_toggle(
                page, "search", sels.toggle_button.get("search", []), search
            )
        if options:
            await self._apply_extra_options(page, options)

    async def _apply_extra_options(self, page: Page, options: dict) -> None:
        """provider 自定义选项（默认忽略）；子类按需覆写。"""
        return None

    async def _set_toggle(self, page: Page, field: str, cands: list[str], want_on: bool) -> bool:
        """设置开关到目标状态；点击后读回校验，不一致重试一次，仍不一致显式告警。

        读回校验不能省：开关点击被 UI 吞掉（重渲染、动画中、遮罩）或状态读错时，
        静默返回会让调用方以为"勾了没生效"。校验+重试可修正两个方向的误判。

        - 页面无此开关（UI 未渲染或该模式没有）→ 记日志并返回 False
        - 返回 True = 页面已确认处于目标状态
        """
        if not cands:
            return False
        sel = await extractor.first_match(page, cands)
        if sel is None:
            logger.info(
                "[%s] toggle %s=%s skipped: 页面无此开关（未渲染或已下线）",
                self.name, field, want_on,
            )
            return False
        for attempt in range(2):
            is_on = await self._is_checked(page, sel, self.cfg.selectors.toggle_checked)
            if is_on == want_on:
                if attempt:
                    logger.info(
                        "[%s] toggle %s 重试后确认 %s", self.name, field,
                        "on" if want_on else "off",
                    )
                else:
                    logger.info(
                        "[%s] toggle %s already %s", self.name, field, "on" if want_on else "off"
                    )
                return True
            logger.info(
                "[%s] toggle %s -> %s%s", self.name, field, "on" if want_on else "off",
                "（重试）" if attempt else "",
            )
            try:
                await page.locator(sel).first.click()
            except Exception as e:  # 元素被遮罩/移除等 → 不静默
                logger.warning("[%s] toggle %s 点击失败: %s", self.name, field, e)
                return False
            await page.wait_for_timeout(800)  # 等 toggle 状态渲染
        is_on = await self._is_checked(page, sel, self.cfg.selectors.toggle_checked)
        if is_on != want_on:
            logger.warning(
                "[%s] toggle %s 未生效：期望 %s，页面读回 %s（点击被 UI 吞掉或状态选择器失配）",
                self.name, field, "on" if want_on else "off", "on" if is_on else "off",
            )
            return False
        return True

    async def _apply_model(self, page: Page, model: str | None, can_set: bool = True) -> None:
        """按 ``selectors.model_menu`` 在页面选择模型（``model`` 名 → ``ui_label``）。

        未配置菜单 / 已是目标 / ``can_set=False``（resume）时不做任何操作。
        """
        menu = self.cfg.selectors.model_menu
        if not menu.trigger or not menu.option or not model:
            return
        if not can_set:
            logger.info("[%s] model=%s ignored (resume)", self.name, model)
            return
        label = model
        for m in self.cfg.models:
            if m.name == model:
                label = m.ui_label or m.name
                break
        await self._select_menu_option(page, menu, label, what="model")

    async def _select_menu_option(self, page: Page, menu, label: str, *, what: str) -> bool:
        """下拉菜单选择：点 trigger → 点 option（``{label}`` 占位）→ 可选读回校验。"""
        if not menu.trigger or not menu.option:
            return False
        # 已是目标值 → 跳过
        if menu.current and label:
            cur_sel = await extractor.first_match(page, menu.current)
            if cur_sel is not None:
                try:
                    cur = (await page.locator(cur_sel).first.inner_text() or "").strip()
                except Exception:
                    cur = ""
                if cur == label:
                    logger.info("[%s] %s already %r", self.name, what, label)
                    return True
        trigger = await extractor.first_match(page, menu.trigger)
        if trigger is None:
            logger.info("[%s] %s menu trigger not found", self.name, what)
            return False
        try:
            await page.locator(trigger).first.click()
            await page.wait_for_timeout(500)
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] %s menu open failed: %s", self.name, what, e)
            return False
        cands = [s.replace("{label}", label) for s in menu.option]
        opt = await extractor.wait_first_match(page, cands, timeout=self.MENU_OPTION_TIMEOUT)
        if opt is None:
            logger.warning(
                "[%s] %s option %r not found (cands=%s)", self.name, what, label, cands
            )
            try:
                await page.keyboard.press("Escape")  # 关掉展开的菜单
            except Exception:
                pass
            return False
        try:
            await page.locator(opt).first.click()
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] %s option click failed: %s", self.name, what, e)
            return False
        await page.wait_for_timeout(500)
        logger.info("[%s] %s -> %r", self.name, what, label)
        return True

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
            return bool(
                await page.locator(sel).first.evaluate("(el, s) => el.matches(s)", checked_sel)
            )
        except Exception:
            return False

    # ---------- 附件 ----------

    async def _upload_attachments(self, page: Page, attachments: list[dict] | None) -> None:
        """发送前把请求附件上传到输入框。

        两种上传方式（配置决定）：
        - ``attachment_menu.trigger`` + ``attachment_menu.item``：点开菜单 → 点菜单项 →
          弹出系统文件选择器（``expect_file_chooser``）→ 设文件（如 Qwen「上传附件」）。
        - 否则：定位文件 input（``attachment_menu.file_input`` / ``upload_input``）后 ``set_input_files``。
        完成后等预览出现（``attachment_menu.preview`` 或通用 blob 图）。
        """
        if not attachments:
            return
        if self.MAX_ATTACHMENTS is not None and len(attachments) > self.MAX_ATTACHMENTS:
            raise AttachmentError(
                f"附件数量 {len(attachments)} 超过上限 {self.MAX_ATTACHMENTS} 个",
                provider=self.name,
            )
        menu = self.cfg.selectors.attachment_menu
        paths, tmpdir = await self._materialize_attachments(attachments)
        try:
            # 可选：先点开菜单（如 Qwen 的 “+”/选择模式）
            if menu.trigger:
                trg = await extractor.first_match(page, menu.trigger)
                if trg is not None:
                    try:
                        await page.locator(trg).first.click()
                        await page.wait_for_timeout(400)
                    except Exception as e:  # noqa: BLE001
                        logger.debug("[%s] attachment menu trigger click failed: %s", self.name, e)
            if menu.item:
                # 菜单项 → 系统文件选择器
                item = await extractor.wait_first_match(
                    page, menu.item, timeout=self.ATTACH_INPUT_TIMEOUT
                )
                if item is None:
                    raise AttachmentError(
                        f'provider "{self.name}" 附件菜单项未找到（attachment_menu.item 均未匹配）',
                        provider=self.name,
                    )
                try:
                    async with page.expect_file_chooser(timeout=10000) as fc:
                        await page.locator(item).first.click()
                    chooser = await fc.value
                    await chooser.set_files(paths)
                except Exception as e:  # noqa: BLE001
                    raise AttachmentError(
                        f'provider "{self.name}" 附件文件选择器未弹出：{e}', provider=self.name
                    ) from None
                logger.info(
                    "[%s] upload %d attachment(s) via menu item %s", self.name, len(paths), item
                )
            else:
                file_cands = (
                    menu.file_input or self.cfg.selectors.upload_input or ["input[type=file]"]
                )
                sel = await extractor.wait_first_match(
                    page, file_cands, timeout=self.ATTACH_INPUT_TIMEOUT
                )
                if sel is None:
                    raise AttachmentError(
                        f'provider "{self.name}" 当前页面未找到附件上传入口'
                        "（attachment_menu.file_input / upload_input 均未匹配，检查配置或页面是否已改版）",
                        provider=self.name,
                    )
                logger.info("[%s] upload %d attachment(s) via %s", self.name, len(paths), sel)
                await page.locator(sel).first.set_input_files(paths)
            await self._wait_attachment_ready(page, len(paths))
        finally:
            import shutil

            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

    async def _materialize_attachments(
        self, attachments: list[dict]
    ) -> tuple[list[str], str | None]:
        """附件 → 临时文件路径（data URL 解码 / http(s) 下载）。"""
        import base64
        import uuid

        paths: list[str] = []
        tmpdir: str | None = None
        for att in attachments:
            name = att.get("name", "image")
            data = att.get("data")
            if data:
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
                    f"附件 {name} 超过上限 {self.MAX_ATTACH_BYTES // (1024 * 1024)}MB",
                    provider=self.name,
                )
            if tmpdir is None:
                tmpdir = os.path.join(tempfile.gettempdir(), f"aiw2a_{uuid.uuid4().hex[:8]}")
                os.makedirs(tmpdir, exist_ok=True)
            safe_name = os.path.basename(name) or "image"  # 防路径穿越
            path = os.path.join(tmpdir, safe_name)
            with open(path, "wb") as f:
                f.write(raw)
            paths.append(path)
        return paths, tmpdir

    async def _wait_attachment_ready(self, page: Page, expected: int) -> None:
        """等输入框上方出现 ``expected`` 个预览。

        ``attachment_menu.preview`` 配了就用它计数；否则用通用 blob 图片计数
        （排除 ``ATTACH_PREVIEW_EXCLUDE`` 容器里的旧图）。
        """
        preview = self.cfg.selectors.attachment_menu.preview
        if preview:
            deadline = time.monotonic() + self.ATTACH_WAIT_SECONDS
            while time.monotonic() < deadline:
                cnt = 0
                for s in preview:
                    try:
                        cnt += await page.locator(s).count()
                    except Exception:
                        pass
                if cnt >= expected:
                    break
                await page.wait_for_timeout(500)
            await page.wait_for_timeout(800)
            return
        ta = page.locator(
            self.cfg.selectors.input[0] if self.cfg.selectors.input else "textarea"
        ).first
        try:
            box = await ta.bounding_box()
        except Exception:
            box = None
        exclude = self.ATTACH_PREVIEW_EXCLUDE or ""
        js = """(args) => {
          const [ty, exclude] = args;
          const imgs = document.querySelectorAll("img[src^='blob:']");
          let n = 0;
          for (const im of imgs) {
            if (exclude && im.closest(exclude)) continue;  // 消息区图片不算
            const r = im.getBoundingClientRect();
            if (r.width > 10 && r.height > 10 && ty !== null &&
                r.y < ty && r.y > ty - 500) n++;
          }
          return n;
        }"""
        deadline = time.monotonic() + self.ATTACH_WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                cnt = await page.evaluate(js, [box["y"] if box else None, exclude])
                if cnt >= expected:
                    break
            except Exception:
                pass
            await page.wait_for_timeout(500)
        await page.wait_for_timeout(800)  # 等上传状态稳定

    @staticmethod
    def _download_attachment(url: str, timeout: float = 30) -> bytes:
        """同步下载 http(s) 附件（asyncio.to_thread 包裹）。"""
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    # ---------- 发送 ----------

    async def _send_prompt(self, page: Page, input_sel: str, prompt: str) -> None:
        cfg = self.cfg
        input_el = page.locator(input_sel).first
        logger.info("[%s] send: click input", self.name)
        await input_el.click()
        if cfg.selectors.type_prompt:
            # contenteditable / React 输入框：逐字输入（fill 可能不触发框架状态）
            logger.info("[%s] send: type", self.name)
            await input_el.fill("")
            await self._type_prompt_human(page, input_el, prompt)
        else:
            logger.info("[%s] send: fill", self.name)
            await input_el.fill(prompt)
        await page.wait_for_timeout(250)  # 等 UI 启用发送
        send_sel = await extractor.first_match(page, cfg.selectors.send_button)
        logger.info("[%s] send: button=%s", self.name, send_sel)
        if send_sel:
            await page.locator(send_sel).first.click()
        else:
            await page.keyboard.press("Enter")

    async def _message_delivered(
        self,
        page: Page,
        md_before: dict[str, int] | None = None,
        th_before: dict[str, int] | None = None,
    ) -> bool:
        """页面是否真的开始生成：出现新的 assistant/thinking 容器，或停止按钮可见。

        用于区分两种情况：消息被静默丢弃（要重发/快速失败） vs 只是生成慢（继续等）。
        """
        for before in (md_before, th_before):
            for sel, count in (before or {}).items():
                try:
                    if await extractor.count_matches(page, sel) > count:
                        return True
                except Exception:  # noqa: BLE001
                    continue
        for stop in self.cfg.selectors.stop_button:
            if await self._is_visible(page, stop):
                return True
        return False

    async def _dismiss_overlays(self, page: Page, *, reason: str = "") -> list[str]:
        """点掉遮挡输入的弹窗/横幅（``selectors.dismiss_button``，出现即点）。

        站点弹窗（繁忙提示、公告、升级引导）会挡住输入框或吞掉点击 —— 实测 Kimi 的
        「和Kimi聊天的人太多了，订阅会员可进入优先队列」就是这类。返回实际点掉的选择器。
        """
        dismissed: list[str] = []
        for sel in getattr(self.cfg.selectors, "dismiss_button", []) or []:
            loc = page.locator(sel)
            try:
                total = await loc.count()
            except Exception:  # noqa: BLE001
                continue
            # ⚠ 必须逐个找"可见的"那个：站点常把弹窗模板多份渲染在 DOM 里，
            # 只取 .first 会点到隐藏的那份（实测 Kimi 弹窗就关不掉）。
            for i in range(min(total, 8)):
                try:
                    item = loc.nth(i)
                    if not await item.is_visible():
                        continue
                    await item.click(timeout=3000)
                    dismissed.append(sel)
                    logger.info(
                        "[%s] 已关闭弹窗%s：%s（第 %d/%d 个匹配）",
                        self.name, f"（{reason}）" if reason else "", sel, i + 1, total,
                    )
                    await page.wait_for_timeout(200)
                    break
                except Exception:  # noqa: BLE001  某个匹配点不动就试下一个
                    continue
        return dismissed

    async def _busy_hint_visible(self, page: Page) -> bool:
        """站点是否在提示"繁忙/排队/限流"（用于给出明确错误）。"""
        for sel in getattr(self.cfg.selectors, "busy_hint", []) or []:
            if await self._is_visible(page, sel):
                return True
        return False

    async def _confirm_sent(
        self, page: Page, input_sel: str, prompt: str, *, timeout: float = 8.0
    ) -> bool:
        """发送后确认输入框已被清空（= 网页端真的接受了这条消息）。

        读法双保险：``input_value()``（textarea/input）+ ``inner_text()``（contenteditable）。
        取不到值时**视为已发送**（不误伤），只有明确还留着原文才判定未发出。
        """
        probe = (prompt or "").strip()[:20]
        deadline = time.monotonic() + timeout
        while True:
            left = ""
            try:
                loc = page.locator(input_sel).first
                try:
                    left = (await loc.input_value() or "").strip()
                except Exception:  # noqa: BLE001  非 input/textarea 会抛
                    pass
                if not left:
                    left = (await loc.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                return True
            if not left or (probe and probe not in left):
                return True
            if time.monotonic() >= deadline:
                return False
            await page.wait_for_timeout(400)

    async def _type_prompt_human(self, page: Page, input_el, prompt: str) -> None:
        """逐字输入提示词（触发 React 受控状态）。

        换行必须用 ``Shift+Enter``：contenteditable / textarea 会把裸 ``Enter`` 当成
        “发送”，多行提示词（function calling 工具清单、markdown）会被截断 → 发送不完整。
        """
        text = prompt.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if line:
                await input_el.press_sequentially(line, delay=15)
            if i < len(lines) - 1:
                await page.keyboard.press("Shift+Enter")

    # ---------- 网络接入 ----------

    # ---------- 观测型旁听（不解析内容，只看时序/状态码） ----------

    @property
    def net_observe(self) -> bool:
        """是否启用"观测型旁听"（``network.observe`` + ``url_pattern``）。"""
        net = self.cfg.network
        return bool(net.observe and net.url_pattern)

    def _observe_net(self, page: Page) -> None:
        """挂上响应监听：记录匹配 ``url_pattern`` 的响应对象（仅观测，不改页面）。"""
        if not self.net_observe:
            return
        pattern = re.compile(self.cfg.network.url_pattern or "")
        events: list = []

        def on_response(resp) -> None:  # noqa: ANN001
            try:
                if pattern.search(resp.url):
                    events.append(resp)
            except Exception:  # noqa: BLE001  响应对象可能已失效
                pass

        page.on("response", on_response)
        self._net_events = events
        self._net_handler = on_response

    def _stop_observe(self, page: Page) -> None:
        handler = getattr(self, "_net_handler", None)
        if handler is not None:
            try:
                page.remove_listener("response", handler)
            except Exception:  # noqa: BLE001
                pass
        self._net_handler = None
        self._net_events = []

    def _net_summary(self) -> str:
        """把最近一次匹配请求的时序概括成一行（超时/完成时都能用）。"""
        events = getattr(self, "_net_events", None) or []
        if not events:
            return "网络旁听：未捕获到匹配请求（可能请求没发出，或 URL 规则不匹配）"
        resp = events[-1]
        timing: dict = {}
        try:
            timing = resp.request.timing or {}
        except Exception:  # noqa: BLE001
            pass
        start = timing.get("requestStart")

        def delta(key: str) -> float | None:
            value = timing.get(key)
            if value is None or start is None or value < 0:
                return None
            return (value - start) / 1000.0

        fmt = lambda v: "—" if v is None else f"{v:.1f}s"  # noqa: E731
        head, end = delta("responseStart"), delta("responseEnd")
        state = "流已结束" if end is not None else "**流未结束**"
        return (
            f"网络旁听：{resp.request.method} {resp.url[:90]} status={resp.status} "
            f"响应头={fmt(head)} 结束={fmt(end)} {state}"
        )

    def _net_init_js(self, url_pattern: str) -> str:
        """生成 XHR 监听脚本：匹配 ``url_pattern`` 的请求把 responseText 存到全局量。"""
        g, d = self.NET_GLOBAL, self.NET_GLOBAL_DONE
        pat = json.dumps(url_pattern)  # 包成 JS 字符串供 new RegExp 用，避免正则转义问题
        return f"""
window.{g} = "";
window.{d} = false;
const __aiw2a_rx = new RegExp({pat});
const __aiw2a_oo = XMLHttpRequest.prototype.open;
const __aiw2a_os = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open = function (method, url) {{
  this.__aiw2a_url = url;
  return __aiw2a_oo.apply(this, arguments);
}};
XMLHttpRequest.prototype.send = function (body) {{
  const xhr = this;
  if (xhr.__aiw2a_url && __aiw2a_rx.test(xhr.__aiw2a_url)) {{
    xhr.addEventListener('readystatechange', function () {{
      try {{
        const rt = xhr.responseText || '';
        if (rt) window.{g} = rt;
        if (xhr.readyState === 4) window.{d} = true;
      }} catch (e) {{}}
    }});
  }}
  return __aiw2a_os.apply(this, arguments);
}};
"""

    def init_scripts(self) -> list[str]:
        """默认：按 ``network.url_pattern`` 生成 XHR 监听；未配 → 不注入（走 DOM）。"""
        if not self.net_enabled:
            return []
        assert self.cfg.network.url_pattern is not None
        return [self._net_init_js(self.cfg.network.url_pattern)]

    @property
    def net_enabled(self) -> bool:
        """是否启用网络抓取（未启用则全程走 DOM，不碰全局量）。"""
        net = self.cfg.network
        return bool(net.capture and net.url_pattern)

    # ---------- 响应读取：网络优先 + DOM 兜底 ----------

    def _reset_net_capture_js(self) -> str:
        g, d = self.NET_GLOBAL, self.NET_GLOBAL_DONE
        return f"() => {{ window.{g} = ''; window.{d} = false; }}"

    async def _reset_net_capture(self, page: Page) -> None:
        try:
            await page.evaluate(self._reset_net_capture_js())
        except Exception:
            pass

    async def _poll_response(
        self,
        page: Page,
        md_before: dict[str, int],
        th_before: dict[str, int],
        busy_timeout: float | None = None,
        *,
        input_sel: str | None = None,
        prompt: str | None = None,
        frames_before: set[str] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """双轨读取：网络监听为主，通道失效自动降级 DOM 轮询。"""
        if not self.net_enabled:
            async for chunk in self._poll_response_dom(
                page, md_before, th_before, busy_timeout,
                input_sel=input_sel, prompt=prompt, frames_before=frames_before,
            ):
                yield chunk
            return
        g, d = self.NET_GLOBAL, self.NET_GLOBAL_DONE
        try:
            injected = await page.evaluate(
                f"() => typeof window.{g} !== 'undefined' && typeof window.{d} !== 'undefined'"
            )
        except Exception:
            injected = False
        if injected:
            try:
                async for chunk in self._poll_response_net(
                    page, md_before, th_before, busy_timeout
                ):
                    yield chunk
                return
            except _NetFallback as e:
                logger.info(
                    "[%s] network capture unavailable (%s), fallback to DOM polling",
                    self.name, e,
                )
        async for chunk in self._poll_response_dom(
            page, md_before, th_before, busy_timeout,
            input_sel=input_sel, prompt=prompt, frames_before=frames_before,
        ):
            yield chunk

    async def _poll_response_net(
        self,
        page: Page,
        md_before: dict[str, int],
        th_before: dict[str, int],
        busy_timeout: float | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """网络监听主路径：快照 → 解析 → diff 增量输出。

        - 结束：SSE ``event: close`` 硬信号
        - 通道失效（宽限期内无事件 / 注入丢失 / 页面导航）→ _NetFallback 降级 DOM
        """
        cfg = self.cfg
        timeout = cfg.response_timeout
        poll_ms = int(cfg.poll_interval * 1000)
        net_grace = (
            cfg.network.grace_seconds
            if cfg.network.grace_seconds is not None
            else min(max(6.0, timeout * 0.2), 20.0)
        )
        start = time.monotonic()

        # 阶段1：等首次事件
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise ResponseTimeoutError(
                    f'provider "{self.name}" 发送后未检测到回复开始'
                    f"（页面无新回复容器：可能被限流/排队，或选择器失配）",
                    provider=self.name,
                )
            if busy_timeout and elapsed > busy_timeout:  # 0/None = 关闭忙检测
                raise ThreadBusyError(
                    f'provider "{self.name}" thread 页面正忙（上一请求未完成，发送被排队），'
                    f"会话已销毁，请稍后重试（thread_busy_timeout={busy_timeout:.0f}s）",
                    provider=self.name,
                )
            sse = await self._net_read(page)
            if sse:
                break
            if elapsed > net_grace:
                raise _NetFallback(f"no SSE within {net_grace:.0f}s")
            if await self._find_new(page, md_before) or await self._find_new(page, th_before):
                raise _NetFallback("DOM moved before SSE")
            await page.wait_for_timeout(poll_ms)

        # 阶段2：流式消费
        last = {"thinking": "", "content": ""}
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                # 兜底：超时前再取一次 —— 页面上往往**已经生成完了**（实测：画面上答案在，
                # 我们却报 180s 超时）。有内容就返回，别把它丢掉；真的空才报错。
                try:
                    final_th = (
                        await self._extract_thinking(page, th_sel, th_idx) if th_sel else ""
                    )
                    final = await self._extract_content(page, md_sel, md_idx, final_th)
                except Exception:  # noqa: BLE001
                    final, final_th = "", ""
                if final.strip() or final_th.strip():
                    logger.warning(
                        "[%s] 超时兜底：已拿到内容（content=%d chars, thinking=%d chars），"
                        "直接返回而不是报超时",
                        self.name, len(final), len(final_th),
                    )
                    if final and final != last["content"]:
                        if buffer_content:
                            yield StreamChunk("content", final)
                        else:
                            inc = _diff_increment(last["content"], final)
                            if inc:
                                yield StreamChunk("content", inc)
                    return
                raise ResponseTimeoutError(
                    f'provider "{self.name}" 响应超时 ({timeout:.0f}s)', provider=self.name
                )
            sse = await self._net_read(page)
            if sse is None:
                raise _NetFallback("capture lost (page navigated?)")
            thinking, content = self._parse_sse_snapshot(sse)
            pairs: list[tuple[Literal["thinking", "content"], str]] = [
                ("thinking", thinking),
                ("content", content),
            ]
            for kind, new in pairs:
                old = last[kind]
                if new == old:
                    continue
                inc = _diff_increment(old, new)
                if inc:
                    yield StreamChunk(kind, inc)
                last[kind] = new
            if "event: close" in sse:
                # close 后可能还有最后一帧：再读一次最新快照 flush 残留增量
                sse2 = await self._net_read(page)
                if sse2:
                    thinking, content = self._parse_sse_snapshot(sse2)
                    pairs2: list[tuple[Literal["thinking", "content"], str]] = [
                        ("thinking", thinking),
                        ("content", content),
                    ]
                    for kind, new in pairs2:
                        old = last[kind]
                        if new != old:
                            inc = _diff_increment(old, new)
                            if inc:
                                yield StreamChunk(kind, inc)
                            last[kind] = new
                return
            await page.wait_for_timeout(poll_ms)

    @classmethod
    def _parse_sse_snapshot(cls, sse_text: str) -> tuple[str, str]:
        """网络快照 → (thinking, content)。默认无解析能力 → 触发 DOM 兜底。"""
        raise _NetFallback("provider 未实现网络流解析")

    async def _net_read(self, page: Page) -> str | None:
        """读取当前网络快照；注入丢失/页面导航 → None。"""
        try:
            v = await page.evaluate(f"() => window.{self.NET_GLOBAL}")
            return v if isinstance(v, str) else None
        except Exception:
            return None

    async def _poll_response_dom(
        self,
        page: Page,
        md_before: dict[str, int],
        th_before: dict[str, int],
        busy_timeout: float | None = None,
        *,
        input_sel: str | None = None,
        prompt: str | None = None,
        frames_before: set[str] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """DOM 兜底路径：增量 diff 响应文本，产出 StreamChunk（thinking 与 content 分开）。

        容器选择器动态发现：先等任一"新增"标记出现，正文容器出现后即自动接管提取。
        """
        cfg = self.cfg
        stop_sels = cfg.selectors.stop_button
        timeout = cfg.response_timeout
        poll_ms = int(cfg.poll_interval * 1000)
        stable_polls = cfg.stable_polls
        min_wait = cfg.min_wait_before_stable
        stop_settle = getattr(cfg.selectors, "stop_settle_polls", 2) or 0
        preview = bool(getattr(cfg.selectors, "preview_stream", False))
        preview_min = max(1, int(getattr(cfg.selectors, "preview_min_chars", 12) or 12))
        # 正文重渲染严重的站点（如 ChatGPT）逐字 diff 会丢内容 → 改“缓冲、结束一次性发”
        buffer_content = not cfg.selectors.stream_content

        # 阶段1：等待新回复标记出现（md 或 thinking 任一候选数量增加）
        md_sel: str | None = None
        th_sel: str | None = None
        start = time.monotonic()
        confirm_timeout = getattr(cfg, "send_confirm_timeout", 0) or 0
        confirmed = False   # 是否已经重发过一次
        busy_seen = False   # 见到"繁忙/排队"提示（关掉后重发一次，仍失败才报繁忙）
        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise ResponseTimeoutError(
                    f'provider "{self.name}" 发送后未检测到回复开始（{timeout:.0f}s）'
                    f"；消息已发出但页面没有新回复容器（可能被限流/排队，或选择器失配）"
                    + (("；" + self._net_summary()) if self.net_observe else ""),
                    provider=self.name,
                )
            # 早期确认：一段时间内既无新容器、也无停止按钮 → 消息很可能是被静默丢弃了。
            # 重发一次；重发后仍无迹象就快速失败，而不是死等满 response_timeout。
            if confirm_timeout and not confirmed and elapsed > confirm_timeout:
                delivered = await self._message_delivered(page, md_before, th_before)
                if not delivered:
                    await self._dismiss_overlays(page, reason="探测到无生成迹象")
                    if await self._busy_hint_visible(page):
                        # 繁忙弹窗会**挡住发送**（实测：文字留在输入框没发出去）
                        # → 先关弹窗，再重发一次，而不是直接报错
                        busy_seen = True
                        await self._dismiss_overlays(page, reason="繁忙提示")
                        logger.warning(
                            "[%s] 站点提示繁忙/排队 → 已关闭提示，重发一次试试", self.name
                        )
                if not delivered and input_sel and prompt:
                    logger.warning(
                        "[%s] %.0fs 内无任何生成迹象 → 判定未发出，重发一次", self.name, elapsed
                    )
                    try:
                        await self._send_prompt(page, input_sel, prompt)
                    except Exception:  # noqa: BLE001
                        logger.warning("[%s] 重发失败", self.name, exc_info=True)
                    start = time.monotonic()          # 重发后重新计时
                    confirmed = True                  # 只重发一次
                    continue
                confirmed = True                      # 有迹象（或无法重发）→ 继续正常等待
                start = time.monotonic()
            md_sel = await self._find_new(page, md_before)
            th_sel = await self._find_new(page, th_before)
            if md_sel or th_sel:
                break
            if confirm_timeout and confirmed and elapsed > confirm_timeout:
                # 已重发过一次且仍无任何迹象 → 快速失败（明确原因，不误导为"超时"）
                if not await self._message_delivered(page, md_before, th_before):
                    if busy_seen:
                        raise ResponseTimeoutError(
                            f'provider "{self.name}" 网站提示繁忙/排队（可能需要订阅优先队列）：'
                            f"已关闭提示并重发，但页面仍未开始生成，请稍后重试",
                            provider=self.name,
                        )
                    raise ResponseTimeoutError(
                        f'provider "{self.name}" 消息未能发出：重发后网页端仍未开始生成'
                        f"（{elapsed:.0f}s，常见于账号限流/风控或页面未就绪），请稍后重试",
                        provider=self.name,
                    )
                start = time.monotonic()
            if busy_timeout and elapsed > busy_timeout:  # 0/None = 关闭忙检测
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
        thinking_changed_recently = 0.0
        stop_seen = False
        # 预览流状态：sent = 已经发给客户端的文本（缓冲模式下用来算"还差多少没发"）
        preview_active = False
        sent = {"thinking": "", "content": ""}

        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                # 带上“卡在哪”的原因，便于排查（站点侧慢/限流 vs 我们没找到容器）
                if stop_seen:
                    hint = "；页面仍显示生成中（站点侧慢或限流，可调大 response_timeout）"
                elif md_sel is None:
                    hint = "；未检测到正文容器（页面可能仍在加载/校验）"
                else:
                    hint = ""
                if self.net_observe:
                    hint += "；" + self._net_summary()
                raise ResponseTimeoutError(
                    f'provider "{self.name}" 响应超时 ({timeout:.0f}s){hint}', provider=self.name
                )

            # 动态发现（正文容器可能在思考结束后才出现）
            if md_sel is None:
                found = await self._find_new(page, md_before)
                if found:
                    md_sel, md_idx = found, md_before[found]
            if th_sel is None:
                found = await self._find_new(page, th_before)
                if found:
                    th_sel, th_idx = found, th_before[found]

            th_text = await self._extract_thinking(page, th_sel, th_idx) if th_sel else ""
            md_text = await self._extract_content(page, md_sel, md_idx, th_text)

            # ⚠ content 与 thinking 分别算稳定：
            # 思考区常有"正在思考中 Ns"这类**持续跳动**的文本，若共用一个计数器，
            # 正文早已稳定也会被不断清零 → 永远判不出"结束"（实测 Kimi 排队时 180s 超时，
            # 但画面上答案其实已经生成）。因此正文的结束判定只看**正文自己**是否稳定。
            changed: dict[str, bool] = {"content": False, "thinking": False}
            pairs: list[tuple[Literal["thinking", "content"], str, str]] = [
                ("thinking", th_text, "thinking"),
                ("content", md_text, "content"),
            ]
            for kind, new, key in pairs:
                prev = last[key]
                if new == prev:
                    continue
                changed[key] = True
                if kind == "content" and buffer_content:
                    last[key] = new      # 先攒着，结束时一次性发（避免重渲染导致 diff 丢内容）
                    continue
                inc = _diff_increment(prev, new)
                if inc:
                    yield StreamChunk(kind, inc)
                # DOM 重渲染导致文本被改写（非子串非前缀）：丢弃本次增量，避免重复输出
                last[key] = new
            stable = 0 if changed["content"] else stable + 1
            if changed["thinking"]:
                thinking_changed_recently = time.monotonic()

            # 结束判定
            done = False
            if stop_sels:
                vis = [await self._is_visible(page, s) for s in stop_sels]
                stop_visible = any(vis)
                if stop_visible:
                    stop_seen = True
                elif stop_seen and stable >= stop_settle:
                    # 出现过停止按钮且已消失 = 生成结束；但**再等 N 拍无变化**才定稿（双确认）：
                    # 站点常在按钮消失后才把最后一拍渲染完 → 立刻取会截断长回答/表格（实测）
                    done = True
            if not done and stable >= stable_polls and elapsed > min_wait:
                if last["content"]:
                    # 正文已开始：markdown 渐进渲染会有超过稳定窗口的停顿，放宽避免截断
                    if stable >= stable_polls * 3:
                        done = True
                elif last["thinking"] and md_sel is not None:
                    # 思考稳定、已有正文容器但正文还没出 → 可能"思考→正文"间隙，大幅放宽。
                    # 用"思考最后一次变动的时间"判断（思考会持续跳动，stable 可能一直被清零）。
                    if time.monotonic() - thinking_changed_recently > stable_polls * 4 * cfg.poll_interval:
                        done = True
                # 否则（正文容器尚未出现）→ 继续等，避免空正文提前结束（Qwen 实测）
            if done:
                logger.info(
                    "[%s] done content=%d chars thinking=%d chars elapsed=%.1fs%s",
                    self.name, len(last["content"]), len(last["thinking"]), elapsed,
                    ("  " + self._net_summary()) if self.net_observe else "",
                )
                if buffer_content:
                    # 收尾兜底：结束判定可能比最后一帧渲染早一拍 → 再等一拍重取一次，取更长的
                    await page.wait_for_timeout(poll_ms + 200)
                    try:
                        final = await self._extract_content(
                            page, md_sel, md_idx, last["thinking"]
                        )
                    except Exception:  # noqa: BLE001
                        final = ""
                    if final and len(final) > len(last["content"]):
                        last["content"] = final
                    self._widgets.extend(
                        await self._capture_widgets(page, frames_before or set())
                    )
                    extra = await self._frame_results(page, frames_before or set())
                    if extra:
                        last["content"] = (last["content"] + "\n\n" + extra).strip()
                    if last["content"]:
                        rest = _suffix_after(sent["content"], last["content"])
                        if rest:
                            yield StreamChunk("content", rest)
                    elif not last["thinking"]:
                        # 零内容：与其返回 200 + 空正文（客户端看起来像"响应丢了"），不如明确报错
                        raise ResponseTimeoutError(
                            f'provider "{self.name}" 本轮没有产生任何回复内容'
                            f"（发送后页面未渲染内容，可能被限流/风控或未真正发出），请重试",
                            provider=self.name,
                        )
                return

            # 预览流：缓冲模式下**一有新增就发**（不再要求"稳定 N 拍"——
            # 站点连续吐字时文本每拍都变，等稳定等于等到停顿才发，用户侧就是"缓冲感"）。
            # 只发前缀追加；非前缀改写（重渲染）丢弃，尾巴由定稿时的 _suffix_after 补。
            if buffer_content and preview and not done:
                if not preview_active and len(last["content"]) >= preview_min:
                    preview_active = True
                    logger.info(
                        "[%s] 预览流开始（已生成 %d 字）", self.name, len(last["content"])
                    )
                if preview_active:
                    for kind in ("thinking", "content"):
                        inc = _pending_increment(sent[kind], last[kind])
                        if inc:
                            sent[kind] = last[kind]
                            yield StreamChunk(kind, inc)  # type: ignore[arg-type]

            await page.wait_for_timeout(poll_ms)

    @staticmethod
    def _looks_like_thinking(part: str, thinking: str) -> bool:
        """该块是否"其实就是思考"（拼接时按内容兜底剔除）。"""
        p, t = part.strip(), (thinking or "").strip()
        if not t or not p:
            return False
        if p == t or p in t or t in p:
            return True
        # 长块取前 40 字比对（提取出来的思考可能被裁剪/重排）
        head = p[:40]
        return bool(head) and head in t

    def _widget_store(self):
        """懒创建组件存储（写 ``profiles/<provider>/widgets/``）。"""
        if self._widget_store_obj is None:
            from ..core.widgets import WidgetStore

            self._widget_store_obj = WidgetStore(self.browser.profiles_dir)
        return self._widget_store_obj

    async def _capture_widgets(self, page: Page, before: set[str]) -> list[dict]:
        """把本轮**新出现**的 iframe 组件抓下来（HTML / 截图，按配置）。

        返回元数据列表（含 id），由调用方（路由）放进 ``widgets`` 字段与历史。
        """
        mode = getattr(self.cfg.selectors, "widget_capture", "none")
        if mode == "none":
            return []
        try:
            frames = [f for f in page.frames if (f.url or "") and f.url not in before
                      and not (f.url or "").startswith("about:")]
        except Exception:  # noqa: BLE001
            return []
        store = self._widget_store()
        out: list[dict] = []
        seen: set[str] = set()          # 同一组件常被镜像渲染多次 → 按内容去重
        for frame in frames:
            html_bytes = png_bytes = None
            try:
                if mode in ("html", "both"):
                    html_bytes = (await frame.content()).encode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            try:
                if mode in ("png", "both"):
                    png_bytes = await frame.locator("body").screenshot(type="png")
            except Exception:  # noqa: BLE001
                pass
            fingerprint = hashlib.sha1((html_bytes or b"") + (png_bytes or b"")).hexdigest()
            if not (html_bytes or png_bytes) or fingerprint in seen:
                continue
            seen.add(fingerprint)
            meta = store.save(self.name, html=html_bytes, png=png_bytes)
            if meta:
                meta["provider"] = self.name      # UI 拼组件 URL 需要
                out.append(meta)
        return out

    def take_widgets(self) -> list[dict]:
        """取走本轮捕获的组件元数据（取后清空，路由用）。"""
        out = list(self._widgets)
        self._widgets = []
        return out

    async def _frame_results(self, page: Page, before: set[str]) -> str:
        """本轮**新出现**的 iframe（交互组件/小部件）→ ``[🧩 交互组件](url)`` + 可见文本。

        Kimi 的「地图规划」这类结果渲染在跨域 iframe（``*.kimi-canvas.com``）里，
        主文档 DOM 提取看不到内容；用 Playwright 的 frame 能读到（默认关闭，配置开）。
        """
        if not getattr(self.cfg.selectors, "include_frames", False):
            return ""
        try:
            frames = list(page.frames)
        except Exception:  # noqa: BLE001
            return ""
        blocks: list[str] = []
        for frame in frames:
            url = (frame.url or "").strip()
            if not url or url in before or url.startswith("about:"):
                continue
            text = ""
            try:
                text = (await frame.locator("body").inner_text(timeout=2000) or "").strip()
            except Exception:  # noqa: BLE001
                pass
            text = re.sub(r"\n{3,}", "\n\n", text)[:1500]
            block = f"[🧩 交互组件]({url})"
            if text:
                block += "\n\n" + text
            blocks.append(block)
        if blocks:
            logger.info("[%s] 附上 %d 个交互组件（iframe）内容", self.name, len(blocks))
        return "\n\n".join(blocks)

    async def _extract_content(
        self, page: Page, sel: str | None, idx: int | None, thinking: str = ""
    ) -> str:
        """正文提取：按配置决定"只取该下标"还是"从该下标起全部拼接"。

        ``response_all_new`` 时按**内容**剔除思考块：Kimi 的思考容器类名/成员会变，
        只靠 CSS ``:not()`` + 下标会错位，把 thinking 拼进正文（实测）。
        """
        if not sel or idx is None:
            return ""
        if not getattr(self.cfg.selectors, "response_all_new", False):
            return await extractor.extract_markdown(page, sel, idx)
        parts = await extractor.extract_markdown_parts(page, sel, idx)
        parts = [p for p in parts if not self._looks_like_thinking(p, thinking)]
        out: list[str] = []
        for part in parts:
            if not out or out[-1] != part:
                out.append(part)
        return "\n\n".join(out)

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

    async def _extract_thinking(self, page: Page, selector: str, index: int | None) -> str:
        """思考区文本；默认取元素 innerText（子类可覆写为特定面板提取）。"""
        if not selector:
            return ""
        js = (
            "(args) => { const [sel, i] = args; const n = document.querySelectorAll(sel);"
            " if (!n.length) return '';"
            " const el = i < 0 ? n[n.length - 1] : n[i];"
            " return el ? (el.innerText || el.textContent || '').trim() : ''; }"
        )
        return await page.evaluate(js, [selector, index if index is not None else -1])
