"""实时画面（Live View）：把 provider 的浏览器页面截成 JPEG / MJPEG 推给 UI。

范围见 docs/LIVE_VIEW.md 的「P1 实施规格」：**只读**（无输入注入 / 无 WS / 无认证）。
要点：
- 不主动创建 context/page —— 没在用的 provider 不会被"看一眼"把浏览器拉起来；
- 每 provider 一个采集循环，多观众共享同一帧（扇出），无人观看自动停；
- 有请求在跑时自动降帧（``live_busy_fps``），避免抢占自动化的 CPU。

**帧来源**：直接用 Playwright 的 ``page.screencast``（CDP ``Page.startScreencast``）——
页面重绘才推帧，空闲几乎零成本（对照：定时截图 5fps ≈ 19% 单核，页面不动也烧）。
screencast 不可用/启动失败 → 自动回退原有定时截图循环。
注意：CDP 默认只给 800×500，必须显式传 ``size=视口``，否则画面会变小。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

from playwright.async_api import Page

logger = logging.getLogger(__name__)

MIN_QUALITY, MAX_QUALITY = 1, 95
MIN_FPS, MAX_FPS = 0.2, 15.0

PageResolver = Callable[[str, str | None], Awaitable[Page | None]]  # (provider, focus thread_id)
BusyChecker = Callable[[str], bool]


@dataclass(frozen=True)
class FrameOptions:
    """截帧参数（第一个观众决定，后来的观众共享）。"""

    quality: int = 50
    fps: float = 5.0
    clip: dict | None = None
    # 裁剪模式："" / "full" = 整页；"last" = **只裁最后一条回复**（原生像素 → 窄栏里也看得清字）
    crop: str = ""
    crop_selector: str | None = None      # "last" 用它定位（provider 的 response_container）
    crop_padding: int = 28                # 四周留白（px）

    def as_dict(self) -> dict:
        return {
            "quality": self.quality, "fps": self.fps, "clip": self.clip,
            "crop": self.crop, "crop_selector": self.crop_selector,
        }


def parse_frame_options(
    *,
    quality: int | str | None = None,
    fps: float | str | None = None,
    clip: str | None = None,
    crop: str | None = None,
    crop_selector: str | None = None,
    crop_padding: int | str | None = None,
    default_quality: int = 50,
    default_fps: float = 5.0,
    default_crop: str = "",
) -> FrameOptions:
    """解析并夹取请求参数；非法值一律回退默认（不报错，画面优先）。"""

    def _num(raw, default, lo, hi):
        if raw is None or raw == "":
            return default
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, val))

    resolved_clip = None
    if clip:
        parts = [p.strip() for p in str(clip).split(",")]
        if len(parts) == 4:
            try:
                x, y, width, height = (int(p) for p in parts)
            except ValueError:
                pass
            else:
                if x >= 0 and y >= 0 and width > 0 and height > 0:
                    resolved_clip = {"x": x, "y": y, "width": width, "height": height}
    # crop：last/auto/msg 都表示"只裁最后一条回复"；full/none/off/空 = 整页
    name = str(crop if crop is not None else default_crop).strip().lower()
    resolved_crop = "last" if name in {"last", "auto", "msg", "message", "answer"} else ""
    return FrameOptions(
        quality=int(_num(quality, default_quality, MIN_QUALITY, MAX_QUALITY)),
        fps=_num(fps, default_fps, MIN_FPS, MAX_FPS),
        clip=resolved_clip,
        crop=resolved_crop,
        crop_selector=crop_selector or None,
        crop_padding=int(_num(crop_padding, 28, 0, 200)),
    )


MJPEG_BOUNDARY = "frame"


def mjpeg_part(frame: bytes, boundary: str = MJPEG_BOUNDARY) -> bytes:
    """把一帧 JPEG 包成 MJPEG 分片（multipart/x-mixed-replace）。"""
    head = (
        f"--{boundary}\r\n"
        f"Content-Type: image/jpeg\r\n"
        f"Content-Length: {len(frame)}\r\n\r\n"
    ).encode()
    return head + frame + b"\r\n"


async def crop_box_for_last(page: Page, selector: str, padding: int) -> dict | None:
    """算出"最后一条回复"的可截区域（含留白，夹到视口内）；拿不到 → None（回退整页）。"""
    try:
        box = await page.locator(selector).last.bounding_box()
    except Exception:  # noqa: BLE001  元素不在/页面在动 → 这帧先整页，下帧再试
        return None
    if not box or box.get("width", 0) < 40 or box.get("height", 0) < 40:
        return None
    vp = page.viewport_size or {"width": 1440, "height": 900}
    # 宽度：夹到视口内（左对齐框的左边缘）
    x = max(0.0, float(box["x"]) - padding)
    width = min(float(vp["width"]) - x, float(box["width"]) + padding * 2)
    # 高度：允许超出视口（Playwright 支持 captureBeyondViewport），但**底部对齐**——
    # 长回答时优先看到"最新写出来的部分"（跟随生成）。
    height = max(160.0, float(box["height"]) + padding * 2)
    bottom = float(box["y"]) + float(box["height"]) + padding
    y = max(0.0, bottom - height)
    if width < 40 or height < 40:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


async def capture(page: Page, opts: FrameOptions, *, timeout_ms: float = 10_000) -> bytes:
    """截一帧 JPEG。**不做服务端缩放**（无图像库；清晰度靠 crop（原生像素）+ quality 控制）。"""
    kwargs: dict = {
        "type": "jpeg",
        "quality": opts.quality,
        "caret": "hide",
        "animations": "disabled",
        "timeout": timeout_ms,
    }
    clip = opts.clip
    if clip is None and opts.crop == "last" and opts.crop_selector:
        clip = await crop_box_for_last(page, opts.crop_selector, opts.crop_padding)
    if clip:
        kwargs["clip"] = clip
    return await page.screenshot(**kwargs)


class _Stream:
    """某 provider 的采集循环状态。"""

    def __init__(self, provider: str, opts: FrameOptions, focus: str | None = None) -> None:
        self.provider = provider
        self.focus = focus
        self.opts = opts
        self.queues: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.last_frame: float | None = None
        self.error: str | None = None
        self.frames = 0
        self.source: str = "timer"          # timer | screencast（诊断用，state 里可见）
        self.cast_q: asyncio.Queue = asyncio.Queue(maxsize=1)   # screencast 推来的帧（只留最新）

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def push(self, frame: bytes | None) -> None:
        """向所有观众推送一帧；慢消费者丢弃旧帧（容量 1）。"""
        for q in list(self.queues):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - 竞争兜底
                    pass
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:  # pragma: no cover
                pass

    def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None
        self.push(None)  # 唤醒所有等待者并让其结束


class LiveController:
    """实时画面采集器：单帧 + 直播扇出（见模块 docstring）。"""

    def __init__(
        self,
        page_resolver: PageResolver,
        *,
        busy_checker: BusyChecker | None = None,
        grace: float = 5.0,
        config=None,
    ) -> None:
        self._resolve = page_resolver
        self._busy = busy_checker or (lambda _p: False)
        self._grace = grace
        self._cfg = config
        self._streams: dict[str, _Stream] = {}

    # ---------- 单帧 ----------

    async def frame_once(
        self, provider: str, opts: FrameOptions, focus: str | None = None
    ) -> bytes | None:
        """截一帧；无可用页面返回 None。``focus`` = 指定 thread_id（不传则取最近使用的页面）。"""
        page = await self._resolve(provider, focus)
        if page is None:
            return None
        return await capture(page, opts)

    async def has_page(self, provider: str, focus: str | None = None) -> bool:
        return await self._resolve(provider, focus) is not None

    # ---------- 直播 ----------

    def stream(self, provider: str, opts: FrameOptions, focus: str | None = None) -> _Stream:
        """取（或创建）采集流；键为 (provider, focus)。已存在时**沿用首个观众的参数**。"""
        key = (provider, focus or "")
        st = self._streams.get(key)
        if st is None or (not st.running and not st.queues):
            st = _Stream(provider, opts, focus)
            self._streams[key] = st
        return st

    async def subscribe(
        self, provider: str, opts: FrameOptions, focus: str | None = None
    ) -> AsyncIterator[bytes]:
        st = self.stream(provider, opts, focus)
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        st.queues.add(queue)
        if not st.running:
            st.task = asyncio.create_task(self._run(st))
        logger.info(
            "live: +观众 provider=%s focus=%s viewers=%d fps=%.1f quality=%d",
            provider, st.focus or "-", len(st.queues), st.opts.fps, st.opts.quality,
        )
        try:
            while True:
                frame = await queue.get()
                if frame is None:  # 采集停止/出错
                    return
                yield frame
        finally:
            st.queues.discard(queue)
            logger.info("live: -观众 provider=%s viewers=%d", provider, len(st.queues))
            if not st.queues:
                asyncio.create_task(self._stop_after_grace(st))

    async def _stop_after_grace(self, st: _Stream) -> None:
        await asyncio.sleep(self._grace)
        if not st.queues:
            st.stop()
            logger.info("live: 停止采集 provider=%s（宽限 %.0fs 内无观众）", st.provider, self._grace)

    def _fps_for(self, st: _Stream) -> float:
        """忙时降帧（避免抢占自动化）。"""
        busy_fps = getattr(self._cfg, "live_busy_fps", 2.0) if self._cfg is not None else 2.0
        return min(st.opts.fps, busy_fps) if self._busy(st.provider) else st.opts.fps

    async def _start_cast(self, page: Page, st: _Stream) -> bool:
        """启动 screencast（帧直接进队列）；失败 → False（回退定时截图）。"""
        try:
            vp = page.viewport_size or {}
            size = {"width": int(vp.get("width", 1440)), "height": int(vp.get("height", 900))}

            def on_frame(frame) -> None:
                data = frame.get("data") if isinstance(frame, dict) else getattr(frame, "data", None)
                if not data:
                    return
                if st.cast_q.full():                    # 只留最新帧（慢消费者不堆积）
                    try:
                        st.cast_q.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover
                        pass
                try:
                    st.cast_q.put_nowait(data)
                except asyncio.QueueFull:  # pragma: no cover
                    pass

            try:
                await page.screencast.start(on_frame=on_frame, quality=st.opts.quality, size=size)
            except Exception as exc:  # noqa: BLE001
                # "Screencast is already started"：多为上一次采集异常结束留下的残留
                # → 先 stop() 再重试一次（否则只能回退到定时截帧）
                if "already started" not in str(exc).lower():
                    raise
                logger.info("live: 清理残留 screencast 后重试 provider=%s", st.provider)
                try:
                    await page.screencast.stop()
                except Exception:  # noqa: BLE001
                    pass
                await page.screencast.start(on_frame=on_frame, quality=st.opts.quality, size=size)
            st.source = "screencast"
            st.last_frame = time.monotonic()
            st.frames += 1
            logger.info("live: 改用 screencast provider=%s（按需推帧）", st.provider)
            return True
        except Exception as exc:  # noqa: BLE001  旧版 Playwright / CDP 不支持 → 回退
            logger.info("live: screencast 不可用（%s），回退定时截图 provider=%s", exc, st.provider)
            return False

    async def _run(self, st: _Stream) -> None:
        page: Page | None = None
        use_cast = False
        try:
            while st.queues:
                page = await self._resolve(st.provider, st.focus)
                if page is None:
                    st.error = "没有可截图的页面"
                    break
                if not use_cast:
                    use_cast = await self._start_cast(page, st)
                    if use_cast:
                        # 先推一帧（CDP 首帧可能还没来；保证 <img> 立刻有画面）
                        try:
                            st.push(await capture(page, st.opts))
                        except Exception:  # noqa: BLE001
                            pass

                if use_cast:
                    try:
                        frame = await asyncio.wait_for(st.cast_q.get(), timeout=3.0)
                    except asyncio.TimeoutError:
                        continue                          # 空闲：等下一次重绘（不截帧，几乎零成本）
                    fps = self._fps_for(st)
                    gap = time.monotonic() - (st.last_frame or 0.0)
                    if gap < 1.0 / fps:                   # 重绘太密 → 按 fps 节流
                        await asyncio.sleep(1.0 / fps - gap)
                    st.last_frame = time.monotonic()
                    st.frames += 1
                    st.error = None
                    st.push(frame)
                else:
                    # 回退：与旧实现一致的定时截帧
                    try:
                        st.push(await capture(page, st.opts))
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        st.error = str(exc)[:200]
                        logger.info("live: 截帧失败 provider=%s: %s", st.provider, exc)
                        break
                    st.last_frame = time.monotonic()
                    st.frames += 1
                    st.error = None
                    await asyncio.sleep(1.0 / self._fps_for(st))
        except asyncio.CancelledError:
            pass
        finally:
            if use_cast and page is not None:
                try:
                    await page.screencast.stop()
                except Exception:  # noqa: BLE001
                    pass
            st.push(None)  # 通知观众结束
            st.task = None

    # ---------- 状态 ----------

    def state(self, provider: str, focus: str | None = None) -> dict:
        st = self._streams.get((provider, focus or ""))
        if st is None:
            return {"streaming": False, "viewers": 0, "fps": None, "quality": None,
                    "last_frame_ago": None, "error": None, "focus": focus}
        return {
            "focus": st.focus,
            "streaming": st.running and bool(st.queues),
            "viewers": len(st.queues),
            "fps": st.opts.fps,
            "quality": st.opts.quality,
            "last_frame_ago": None if st.last_frame is None else round(time.monotonic() - st.last_frame, 2),
            "source": st.source,
            "error": st.error,
        }

    def stop_all(self) -> None:
        for st in self._streams.values():
            st.stop()
        self._streams.clear()
