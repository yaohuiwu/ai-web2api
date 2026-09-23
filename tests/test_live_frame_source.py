"""实时画面帧来源：screencast 当"变化信号" + 高清截图出帧；失败自动回退定时截图。

背景（实测）：定时 `page.screenshot()` 5fps ≈ 19% 单核（页面不动也烧）；
CDP screencast 按需推帧（空闲≈0），但输出上限 1x CSS 像素 → 会丢 device_scale_factor=2 的锐度，
所以只把它当**触发器**。设计/数据见 docs/LIVE_VIEW.md。

运行：.venv/bin/python -m pytest tests/test_live_frame_source.py -v
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ai_web2api.browser import live as live_mod
from ai_web2api.browser.live import FrameOptions, LiveController


class _FakeScreencast:
    def __init__(self, boom: bool = False) -> None:
        self.started = 0
        self.stopped = 0
        self.on_frame = None
        self.quality = None
        self.size = None
        self._boom = boom

    async def start(self, on_frame=None, quality=None, size=None):
        if getattr(self, "fail_first", False):
            self.fail_first = False
            raise RuntimeError("Screencast is already started")
        if self._boom:
            raise RuntimeError("CDP 不支持 screencast")
        self.started += 1
        self.on_frame = on_frame
        self.quality = quality
        self.size = size
        return None

    async def stop(self):
        self.stopped += 1

    def fire(self, n: int = 1, data: bytes = b"JPEG-DATA"):
        for _ in range(n):
            if self.on_frame:
                self.on_frame({"data": data, "timestamp": time.time()})


class _FakePage:
    def __init__(self, boom: bool = False) -> None:
        self.screencast = _FakeScreencast(boom=boom)
        self.viewport_size = {"width": 1440, "height": 900}
        self.screenshots = 0

    async def screenshot(self, **_kw):
        self.screenshots += 1
        return b"JPEG-DATA"


class _Cfg:
    live_busy_fps = 2.0
    live_idle_keepalive_seconds = 0.15


async def _drain(st, n: int, timeout: float = 2.0) -> list[bytes]:
    """从流的队列里收 n 帧（None 表示结束）。"""
    q = asyncio.Queue(maxsize=1)
    st.queues.add(q)
    out: list[bytes] = []
    try:
        while len(out) < n:
            frame = await asyncio.wait_for(q.get(), timeout=timeout)
            if frame is None:
                break
            out.append(frame)
    finally:
        st.queues.discard(q)
    return out


@pytest.mark.asyncio
async def test_screencast_frames_are_pushed_to_viewers(monkeypatch):
    """screencast 推来的帧**直接**发给观众（不再自建定时截帧）。"""
    page = _FakePage()
    ctl = LiveController(lambda p, f: _async(page), grace=0.05, config=_Cfg())
    st = ctl.stream("deepseek", FrameOptions(fps=50, quality=75))

    monkeypatch.setattr(live_mod, "capture", lambda p, o, **kw: page.screenshot())
    st.queues.add(asyncio.Queue(maxsize=1))          # 有观众，采集循环才会跑
    task = asyncio.create_task(ctl._run(st))
    try:
        await asyncio.sleep(0.05)
        assert page.screencast.started == 1
        assert page.screencast.size == {"width": 1440, "height": 900}, "必须显式传视口（否则只有 800x500）"
        q = asyncio.Queue(maxsize=1)
        st.queues.add(q)                              # 先挂上观众，再触发重绘
        page.screencast.fire(1)                       # 页面重绘 → 推来一帧
        frame = await asyncio.wait_for(q.get(), timeout=2)
        assert frame == b"JPEG-DATA", "应把 screencast 帧发给观众"
        assert st.source == "screencast"
    finally:
        st.queues.clear()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_idle_does_not_periodically_screenshot(monkeypatch):
    """空闲（无重绘）时不再**按帧率**周期性截帧（只有 keepalive 会补帧）。"""
    page = _FakePage()
    cfg = _Cfg()
    cfg.live_idle_keepalive_seconds = 10        # 把 keepalive 关掉，专门验证"不按 fps 截帧"
    ctl = LiveController(lambda p, f: _async(page), grace=0.05, config=cfg)
    st = ctl.stream("deepseek", FrameOptions(fps=50, quality=75))

    monkeypatch.setattr(live_mod, "capture", lambda p, o, **kw: page.screenshot())
    st.queues.add(asyncio.Queue(maxsize=1))
    task = asyncio.create_task(ctl._run(st))
    try:
        await asyncio.sleep(0.5)
        assert page.screenshots <= 1, f"空闲不该周期性截帧（实际 {page.screenshots} 次）"
    finally:
        st.queues.clear()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_fallback_to_timer_when_screencast_unavailable(monkeypatch):
    """screencast 启动失败（旧版/CDP 不支持）→ 自动回退定时截图，直播不中断。"""
    page = _FakePage(boom=True)
    ctl = LiveController(lambda p, f: _async(page), grace=0.05, config=_Cfg())
    st = ctl.stream("deepseek", FrameOptions(fps=50, quality=75))

    monkeypatch.setattr(live_mod, "capture", lambda p, o, **kw: page.screenshot())
    st.queues.add(asyncio.Queue(maxsize=1))
    task = asyncio.create_task(ctl._run(st))
    try:
        frames = await _drain(st, 2)
        assert len(frames) == 2, "回退路径必须继续出帧"
        assert st.source == "timer"
        assert page.screencast.started == 0
    finally:
        st.queues.clear()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_screencast_stopped_when_stream_ends(monkeypatch):
    """观众走了 → 采集停止时必须 stop() screencast（否则每条流泄漏一个 CDP 订阅）。"""
    page = _FakePage()
    ctl = LiveController(lambda p, f: _async(page), grace=0.05, config=_Cfg())
    st = ctl.stream("deepseek", FrameOptions(fps=50, quality=75))

    monkeypatch.setattr(live_mod, "capture", lambda p, o, **kw: page.screenshot())
    q = asyncio.Queue(maxsize=1)
    st.queues.add(q)
    task = asyncio.create_task(ctl._run(st))
    st.task = task                                   # subscribe() 里也是这么挂的
    await asyncio.sleep(0.1)
    st.queues.clear()
    st.stop()                                        # 生产里由"无观众 + grace"触发
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2)
    assert page.screencast.stopped == 1


async def _async(value):
    return value


def test_state_exposes_frame_source():
    """状态里能看到帧来源（诊断"为什么在烧 CPU"一眼可见）。"""
    ctl = LiveController(lambda p, f: _async(None), config=_Cfg())
    st = ctl.stream("deepseek", FrameOptions())
    st.source = "screencast"
    assert ctl.state("deepseek")["source"] == "screencast"


@pytest.mark.asyncio
async def test_stale_screencast_is_stopped_and_retried(monkeypatch):
    """残留的 screencast（"already started"）→ 先 stop() 再重试，而不是直接回退定时截图。"""
    page = _FakePage()
    page.screencast.fail_first = True                    # 第一次 start 报"已启动"
    ctl = LiveController(lambda p, f: _async(page), grace=0.05, config=_Cfg())
    st = ctl.stream("deepseek", FrameOptions(fps=50, quality=75))

    monkeypatch.setattr(live_mod, "capture", lambda p, o, **kw: page.screenshot())
    st.queues.add(asyncio.Queue(maxsize=1))
    task = asyncio.create_task(ctl._run(st))
    try:
        await asyncio.sleep(0.05)
        assert page.screencast.started == 1, "重试后应成功启动"
        assert page.screencast.stopped >= 1, "应先清理残留"
        assert st.source == "screencast"
    finally:
        st.queues.clear()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_page_screencast_is_not_stolen_by_second_stream(monkeypatch):
    """screencast 每页只能一份：第二个流（同页、不同 focus）必须回退定时截图，而不是抢。

    实测事故：同时开着 Playground 画面与「画面」页（锁不同会话）→ 两条流互抢 screencast
    → 画面被反复打断（日志里 -观众/+观众 交替）。
    """
    page = _FakePage()
    ctl = LiveController(lambda p, f: _async(page), grace=0.05, config=_Cfg())
    monkeypatch.setattr(live_mod, "capture", lambda p, o, **kw: p.screenshot())

    st1 = ctl.stream("deepseek", FrameOptions(fps=50, quality=75), focus="t1")
    st2 = ctl.stream("deepseek", FrameOptions(fps=50, quality=75), focus="t2")
    st1.queues.add(asyncio.Queue(maxsize=1))
    st2.queues.add(asyncio.Queue(maxsize=1))
    t1 = asyncio.create_task(ctl._run(st1))
    st1.task = t1
    await asyncio.sleep(0.1)
    assert st1.source == "screencast" and page.screencast.started == 1

    t2 = asyncio.create_task(ctl._run(st2))
    st2.task = t2
    await asyncio.sleep(0.1)
    assert st2.source == "timer", "第二个流不能抢 screencast（否则互相打断）"
    assert page.screencast.started == 1, "不应再次 start()"

    for st, task in ((st1, t1), (st2, t2)):
        st.queues.clear()
        st.stop()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_idle_keepalive_pushes_frame(monkeypatch):
    """空闲兜底：长时间没有 screencast 帧时补一帧（避免画面看起来"断了"）。"""
    page = _FakePage()
    cfg = _Cfg()
    cfg.live_idle_keepalive_seconds = 0.1
    ctl = LiveController(lambda p, f: _async(page), grace=0.05, config=cfg)
    st = ctl.stream("deepseek", FrameOptions(fps=50, quality=75))
    monkeypatch.setattr(live_mod, "capture", lambda p, o, **kw: p.screenshot())
    st.queues.add(asyncio.Queue(maxsize=1))
    task = asyncio.create_task(ctl._run(st))
    try:
        await asyncio.sleep(0.05)
        before = page.screenshots
        await asyncio.sleep(0.4)                      # 空闲 0.4s，keepalive=0.1s → 应补帧
        assert page.screenshots > before, "空闲应有 keepalive 补帧"
    finally:
        st.queues.clear()
        st.stop()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cast_failure_is_not_retried_every_tick(monkeypatch):
    """screencast 不可用/被占用时只尝试一次（否则每拍都 start() → 刷日志 + 白开销）。"""
    page = _FakePage(boom=True)
    ctl = LiveController(lambda p, f: _async(page), grace=0.05, config=_Cfg())
    st = ctl.stream("deepseek", FrameOptions(fps=50, quality=75))
    monkeypatch.setattr(live_mod, "capture", lambda p, o, **kw: p.screenshot())

    calls = {"n": 0}

    async def counting_start(**_kw):
        calls["n"] += 1
        raise RuntimeError("Screencast is already started")

    page.screencast.start = counting_start  # type: ignore[assignment]
    st.queues.add(asyncio.Queue(maxsize=1))
    task = asyncio.create_task(ctl._run(st))
    try:
        await asyncio.sleep(0.4)
        first = calls["n"]
        assert first <= 2, f"最多一次清理重试，实际 {first} 次"
        await asyncio.sleep(0.4)                        # 再等一轮：不应继续重试
        assert calls["n"] == first, f"不该每拍重试（{first} → {calls['n']}）"
        assert st.cast_disabled is True and st.source == "timer"
    finally:
        st.queues.clear()
        st.stop()
        await asyncio.gather(task, return_exceptions=True)
