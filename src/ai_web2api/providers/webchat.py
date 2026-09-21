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
import json
import logging
import os
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

            if resume:
                # 会话绑定续用：页面已有完整历史（页面为准），只发最后一条 user 消息
                prompt = last_user_message(messages)
            else:
                # 无状态 / create：完整历史拼单条 prompt 注入
                prompt = build_prompt(messages)
            logger.info("[%s] sending prompt (%d chars): %.60r", self.name, len(prompt), prompt)
            if self.net_enabled:
                await self._reset_net_capture(page)
            await self._send_prompt(page, input_sel, prompt)
            logger.info("[%s] prompt sent, polling", self.name)

            async for chunk in self._poll_response(page, md_before, th_before, busy_timeout):
                yield chunk
        finally:
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

        - data URL → base64 解码写临时文件；http(s) URL → 下载后上传
        - 上传入口：``selectors.upload_input``（默认 ``input[type=file]``），找不到 → 400
        - 上传完成后等预览出现（``ATTACH_PREVIEW_EXCLUDE`` 用于排除消息区旧图）
        """
        if not attachments:
            return
        if self.MAX_ATTACHMENTS is not None and len(attachments) > self.MAX_ATTACHMENTS:
            raise AttachmentError(
                f"附件数量 {len(attachments)} 超过上限 {self.MAX_ATTACHMENTS} 个",
                provider=self.name,
            )
        menu = self.cfg.selectors.attachment_menu
        # 文件 input 候选：attachment_menu.file_input 优先，其次 upload_input
        file_cands = menu.file_input or self.cfg.selectors.upload_input or ["input[type=file]"]
        # 可选：先点开“+”菜单（有些站点展开后才渲染 input）
        if menu.trigger:
            trg = await extractor.first_match(page, menu.trigger)
            if trg is not None:
                try:
                    await page.locator(trg).first.click()
                    await page.wait_for_timeout(300)
                except Exception as e:  # noqa: BLE001
                    logger.debug("[%s] attachment menu trigger click failed: %s", self.name, e)
        sel = await extractor.wait_first_match(page, file_cands, timeout=self.ATTACH_INPUT_TIMEOUT)
        if sel is None:
            raise AttachmentError(
                f'provider "{self.name}" 当前页面未找到附件上传入口'
                "（attachment_menu.file_input / upload_input 均未匹配，检查配置或页面是否已改版）",
                provider=self.name,
            )
        paths: list[str] = []
        tmpdir: str | None = None
        try:
            import base64
            import uuid

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
                    tmpdir = os.path.join(
                        tempfile.gettempdir(), f"aiw2a_{uuid.uuid4().hex[:8]}"
                    )
                    os.makedirs(tmpdir, exist_ok=True)
                safe_name = os.path.basename(name) or "image"  # 防路径穿越
                path = os.path.join(tmpdir, safe_name)
                with open(path, "wb") as f:
                    f.write(raw)
                paths.append(path)
            logger.info("[%s] upload %d attachment(s) via %s", self.name, len(paths), sel)
            await page.locator(sel).first.set_input_files(paths)
            await self._wait_attachment_ready(page, len(paths))
        finally:
            import shutil

            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)

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
        logger.info("[%s] send: fill", self.name)
        await input_el.fill(prompt)
        await page.wait_for_timeout(250)  # 等 UI 启用发送
        send_sel = await extractor.first_match(page, cfg.selectors.send_button)
        logger.info("[%s] send: button=%s", self.name, send_sel)
        if send_sel:
            await page.locator(send_sel).first.click()
        else:
            await page.keyboard.press("Enter")

    # ---------- 网络接入 ----------

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
    ) -> AsyncIterator[StreamChunk]:
        """双轨读取：网络监听为主，通道失效自动降级 DOM 轮询。"""
        if not self.net_enabled:
            async for chunk in self._poll_response_dom(page, md_before, th_before, busy_timeout):
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
        async for chunk in self._poll_response_dom(page, md_before, th_before, busy_timeout):
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
                    f'provider "{self.name}" 发送后未检测到回复开始', provider=self.name
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
        stop_seen = False

        while True:
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise ResponseTimeoutError(
                    f'provider "{self.name}" 响应超时 ({timeout:.0f}s)', provider=self.name
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

            md_text = (
                await extractor.extract_markdown(page, md_sel, md_idx) if md_sel else ""
            )
            th_text = await self._extract_thinking(page, th_sel, th_idx) if th_sel else ""

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
                inc = _diff_increment(old, new)
                if inc:
                    yield StreamChunk(kind, inc)
                # DOM 重渲染导致文本被改写（非子串非前缀）：丢弃本次增量，避免重复输出
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
                    # 思考已产出但正文未开始：可能是"思考→正文"的间隙，大幅放宽
                    if stable >= stable_polls * 4:
                        done = True
                elif last["content"]:
                    # 正文已开始：markdown 渐进渲染会有超过稳定窗口的停顿，放宽避免截断
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
