"""实时画面 P1（只读）：参数解析 / 单帧 / 扇出 / 自动停 / 403 / 503。

不需要真浏览器：page 用 stub，capture 用假实现。
运行：.venv/bin/python -m pytest tests/test_live_view.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ai_web2api.browser.live import FrameOptions, LiveController, parse_frame_options
from ai_web2api.browser.manager import BrowserManager
from ai_web2api.core.queue import SerialGate

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


class _StubPage:
    def __init__(self, url: str = "https://www.kimi.com/chat/abc") -> None:
        self.url = url
        self.viewport_size = {"width": 1440, "height": 900}
        self.shots = 0
        self.last_kwargs: dict = {}

    def is_closed(self) -> bool:
        return False

    async def screenshot(self, **kwargs) -> bytes:
        self.shots += 1
        self.last_kwargs = kwargs
        return JPEG


class _StubCtx:
    def __init__(self, page: _StubPage) -> None:
        self.pages = [page]


# ---------- 参数解析 ----------


def test_parse_frame_options_clamps_and_parses_clip():
    o = parse_frame_options(quality=999, fps=99, clip="1,2,3,4")
    assert (o.quality, o.fps) == (95, 15.0)
    assert o.clip == {"x": 1, "y": 2, "width": 3, "height": 4}
    assert parse_frame_options(quality=0).quality == 1
    assert parse_frame_options(fps=0).fps == 0.2
    # 非法 clip 一律忽略（不报错，画面优先）
    for bad in ("bad", "1,2,3", "-1,0,10,10", "0,0,0,10", "a,b,c,d"):
        assert parse_frame_options(clip=bad).clip is None
    d = parse_frame_options()
    assert (d.quality, d.fps, d.clip) == (50, 5.0, None)
    assert parse_frame_options(quality="x", fps="").quality == 50


# ---------- 采集器 ----------


@pytest.mark.asyncio
async def test_serial_gate_busy_flag():
    gate = SerialGate(max_waiters=2, timeout=2)
    assert gate.busy is False
    started = asyncio.Event()
    release = asyncio.Event()

    async def job() -> str:
        started.set()
        await release.wait()
        return "ok"

    task = asyncio.create_task(gate.run(job))
    await started.wait()
    assert gate.busy is True          # 执行中 → 直播该降帧
    release.set()
    assert await task == "ok"
    assert gate.busy is False


@pytest.mark.asyncio
async def test_frame_once_without_page_is_none():
    async def resolver(_name: str):
        return None

    live = LiveController(resolver)
    assert await live.frame_once("p", FrameOptions()) is None
    assert await live.has_page("p") is False


@pytest.mark.asyncio
async def test_frame_once_passes_options_to_screenshot():
    page = _StubPage()

    async def resolver(_name: str):
        return page

    live = LiveController(resolver)
    frame = await live.frame_once("p", parse_frame_options(quality=33, clip="0,0,320,200"))
    assert frame == JPEG and page.shots == 1
    assert page.last_kwargs["quality"] == 33
    assert page.last_kwargs["clip"] == {"x": 0, "y": 0, "width": 320, "height": 200}
    assert page.last_kwargs["type"] == "jpeg"


@pytest.mark.asyncio
async def test_fanout_shares_one_capture_per_tick_and_autostops():
    """多观众共享同一帧（扇出），全部离开后自动停。"""
    page = _StubPage()

    async def resolver(_name: str):
        return page

    live = LiveController(resolver, grace=0.1)
    seen = {"a": 0, "b": 0}

    async def viewer(key: str, want: int):
        async for _frame in live.subscribe("p", FrameOptions(fps=15)):
            seen[key] += 1
            if seen[key] >= want:
                return

    await asyncio.gather(viewer("a", 4), viewer("b", 4))
    delivered = seen["a"] + seen["b"]
    assert page.shots >= 3
    assert page.shots < delivered, "两人看同一帧，截图次数必须少于投递次数（扇出生效）"
    assert live.state("p")["viewers"] == 0
    await asyncio.sleep(0.35)                    # 宽限后应停
    assert live.state("p")["streaming"] is False
    shots_after = page.shots
    await asyncio.sleep(0.3)
    assert page.shots == shots_after, "无人观看时必须停止采集（零开销）"


@pytest.mark.asyncio
async def test_busy_downgrades_fps():
    """有请求在跑 → 自动降到 1 fps（避免抢占自动化的 CPU）。"""
    page = _StubPage()

    async def resolver(_name: str):
        return page

    live = LiveController(resolver, busy_checker=lambda _p: True, grace=0.05)

    async def viewer():
        async for _f in live.subscribe("p", FrameOptions(fps=15)):
            await asyncio.sleep(0.35)
            return

    await viewer()
    assert page.shots <= 3, f"busy 时 0.35s 内不该截 {page.shots} 次（应为 ~1fps）"


@pytest.mark.asyncio
async def test_stream_stops_when_page_disappears():
    holder: dict = {"page": _StubPage()}

    async def resolver(_name: str):
        return holder["page"]

    live = LiveController(resolver, grace=0.05)
    got = 0
    async for _f in live.subscribe("p", FrameOptions(fps=15)):
        got += 1
        holder["page"] = None            # 页面消失 → 循环应结束并通知观众
    assert got >= 1
    assert live.state("p")["error"] == "没有可截图的页面"


# ---------- 路由 ----------


def _app(tmp_path: Path, *, live_view: bool = True):
    from ai_web2api.main import create_app

    root = Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((root / "config.fake.yaml").read_text())
    cfg["profiles_dir"] = str(tmp_path)
    cfg.setdefault("server", {})["live_view"] = live_view
    cfg["providers"]["fake"]["url"] = f"file://{root / 'tests' / 'fake_chat.html'}"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return TestClient(create_app(str(cfg_path)))


def test_screen_endpoints_disabled_when_live_view_false(tmp_path: Path):
    c = _app(tmp_path, live_view=False)
    for path in ("/admin/fake/screen.jpg", "/admin/fake/screen/state"):
        r = c.get(path)
        assert r.status_code == 403, path
        assert "live_view" in r.json()["error"]["message"]


def test_screen_jpg_unknown_provider(tmp_path: Path):
    assert _app(tmp_path).get("/admin/nope/screen.jpg").status_code == 404


def test_screen_jpg_without_page_returns_503(tmp_path: Path):
    r = _app(tmp_path).get("/admin/fake/screen.jpg")
    assert r.status_code == 503
    assert "没有可截图的页面" in r.json()["error"]["message"]


def test_screen_jpg_and_state_with_page(tmp_path: Path, monkeypatch):
    page = _StubPage()
    monkeypatch.setattr(BrowserManager, "active_context", lambda self, name: _StubCtx(page))

    async def fake_capture(p, opts, **kw):
        return await p.screenshot(quality=opts.quality, clip=opts.clip)

    monkeypatch.setattr("ai_web2api.browser.live.capture", fake_capture)

    c = _app(tmp_path)
    r = c.get("/admin/fake/screen.jpg", params={"quality": 33, "clip": "0,0,320,200"})
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"
    assert page.last_kwargs["quality"] == 33

    state = c.get("/admin/fake/screen/state").json()
    assert state["available"] is True
    assert state["page_url"] == page.url
    assert state["viewport"] == {"width": 1440, "height": 900}
    assert state["streaming"] is False and state["viewers"] == 0
    assert "busy" in state


# ---------- MJPEG ----------


def test_mjpeg_part_framing():
    from ai_web2api.browser.live import mjpeg_part

    part = mjpeg_part(JPEG)
    assert part.startswith(b"--frame\r\nContent-Type: image/jpeg\r\n")
    assert f"Content-Length: {len(JPEG)}".encode() in part
    assert part.endswith(JPEG + b"\r\n")


def test_stream_without_page_returns_503(tmp_path: Path):
    r = _app(tmp_path).get("/admin/fake/stream.mjpg")
    assert r.status_code == 503 and "没有可截图的页面" in r.json()["error"]["message"]


def test_stream_disabled_returns_403(tmp_path: Path):
    assert _app(tmp_path, live_view=False).get("/admin/fake/stream.mjpg").status_code == 403


def test_stream_mjpg_sends_frames(tmp_path: Path, monkeypatch):
    """路由 + 分帧：把订阅替换成有界帧流（真 MJPEG 永不结束，TestClient 会等不到响应结束）。"""
    page = _StubPage()
    monkeypatch.setattr(BrowserManager, "active_context", lambda self, name: _StubCtx(page))

    async def bounded_subscribe(self, provider, opts):
        for _ in range(2):
            yield JPEG

    monkeypatch.setattr(LiveController, "subscribe", bounded_subscribe)

    r = _app(tmp_path).get("/admin/fake/stream.mjpg", params={"fps": 10})
    assert r.status_code == 200
    assert "multipart/x-mixed-replace" in r.headers["content-type"]
    assert r.headers["x-accel-buffering"] == "no"
    body = r.content
    assert body.count(b"--frame\r\nContent-Type: image/jpeg\r\n") == 2
    assert body.count(JPEG) == 2
