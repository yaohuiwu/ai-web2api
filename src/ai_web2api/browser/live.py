"""实时画面（Live View）：把 provider 的浏览器页面截成 JPEG / MJPEG 推给 UI。

范围见 docs/LIVE_VIEW.md 的「P1 实施规格」：**只读**（无输入注入 / 无 WS / 无认证）。
要点：
- 不主动创建 context/page —— 没在用的 provider 不会被"看一眼"把浏览器拉起来；
- 每 provider 一个采集循环，多观众共享同一帧（扇出），无人观看自动停；
- 有请求在跑时自动降帧，避免抢占自动化的 CPU。
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

PageResolver = Callable[[str], Awaitable[Page | None]]
BusyChecker = Callable[[str], bool]


@dataclass(frozen=True)
class FrameOptions:
    """截帧参数（第一个观众决定，后来的观众共享）。"""

    quality: int = 50
    fps: float = 5.0
    clip: dict | None = None

    def as_dict(self) -> dict:
        return {"quality": self.quality, "fps": self.fps, "clip": self.clip}


def parse_frame_options(
    *,
    quality: int | str | None = None,
    fps: float | str | None = None,
    clip: str | None = None,
    default_quality: int = 50,
    default_fps: float = 5.0,
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
    return FrameOptions(
        quality=int(_num(quality, default_quality, MIN_QUALITY, MAX_QUALITY)),
        fps=_num(fps, default_fps, MIN_FPS, MAX_FPS),
        clip=resolved_clip,
    )


async def capture(page: Page, opts: FrameOptions, *, timeout_ms: float = 10_000) -> bytes:
    """截一帧 JPEG。**不做服务端缩放**（无图像库；带宽靠 quality/clip 控制）。"""
    kwargs: dict = {
        "type": "jpeg",
        "quality": opts.quality,
        "caret": "hide",
        "animations": "disabled",
        "timeout": timeout_ms,
    }
    if opts.clip:
        kwargs["clip"] = opts.clip
    return await page.screenshot(**kwargs)


class _Stream:
    """某 provider 的采集循环状态。"""

    def __init__(self, provider: str, opts: FrameOptions) -> None:
        self.provider = provider
        self.opts = opts
        self.queues: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.last_frame: float | None = None
        self.error: str | None = None
        self.frames = 0

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
    ) -> None:
        self._resolve = page_resolver
        self._busy = busy_checker or (lambda _p: False)
        self._grace = grace
        self._streams: dict[str, _Stream] = {}

    # ---------- 单帧 ----------

    async def frame_once(self, provider: str, opts: FrameOptions) -> bytes | None:
        """截一帧；无可用页面返回 None。"""
        page = await self._resolve(provider)
        if page is None:
            return None
        return await capture(page, opts)

    async def has_page(self, provider: str) -> bool:
        return await self._resolve(provider) is not None

    # ---------- 直播 ----------

    def stream(self, provider: str, opts: FrameOptions) -> _Stream:
        """取（或创建）该 provider 的采集流；已存在时**沿用首个观众的参数**。"""
        st = self._streams.get(provider)
        if st is None or (not st.running and not st.queues):
            st = _Stream(provider, opts)
            self._streams[provider] = st
        return st

    async def subscribe(self, provider: str, opts: FrameOptions) -> AsyncIterator[bytes]:
        st = self.stream(provider, opts)
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        st.queues.add(queue)
        if not st.running:
            st.task = asyncio.create_task(self._run(st))
        logger.info(
            "live: +观众 provider=%s viewers=%d fps=%.1f quality=%d",
            provider, len(st.queues), st.opts.fps, st.opts.quality,
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

    async def _run(self, st: _Stream) -> None:
        try:
            while st.queues:
                fps = min(st.opts.fps, 1.0) if self._busy(st.provider) else st.opts.fps
                try:
                    page = await self._resolve(st.provider)
                    if page is None:
                        st.error = "没有可截图的页面"
                        break
                    frame = await capture(page, st.opts)
                    st.last_frame = time.monotonic()
                    st.frames += 1
                    st.error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    st.error = str(exc)[:200]
                    logger.info("live: 截帧失败 provider=%s: %s", st.provider, exc)
                    break
                st.push(frame)
                await asyncio.sleep(1.0 / fps)
        except asyncio.CancelledError:
            pass
        finally:
            st.push(None)  # 通知观众结束
            st.task = None

    # ---------- 状态 ----------

    def state(self, provider: str) -> dict:
        st = self._streams.get(provider)
        if st is None:
            return {"streaming": False, "viewers": 0, "fps": None, "quality": None,
                    "last_frame_ago": None, "error": None}
        return {
            "streaming": st.running and bool(st.queues),
            "viewers": len(st.queues),
            "fps": st.opts.fps,
            "quality": st.opts.quality,
            "last_frame_ago": None if st.last_frame is None else round(time.monotonic() - st.last_frame, 2),
            "error": st.error,
        }

    def stop_all(self) -> None:
        for st in self._streams.values():
            st.stop()
        self._streams.clear()
