"""事件驱动抓取（capture_mode=observer）：等待原语、世代号、回退、注入幂等。

设计见 docs/OBSERVER_CAPTURE.md。核心原则：observer **只替换"等待下一拍"**，
提取/diff/预览流/结束判定全部复用原逻辑；任何异常都回退定时轮询。

运行：.venv/bin/python -m pytest tests/test_observer_capture.py -v
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ai_web2api.browser import extractor as ex
from ai_web2api.config import ProviderConfig
from ai_web2api.core.timeline import RequestTimeline
from ai_web2api.providers.webchat import WebChatProvider


class _StubBrowser:
    default_locale = "zh-CN"


class _NoLoc:
    @property
    def first(self):
        return self

    async def is_visible(self):
        return False


class _FakePage:
    """够用的假页面：等待、locator、expose_function、evaluate。"""

    def __init__(self) -> None:
        self.evaluated: list[tuple] = []
        self.exposed: list[str] = []

    async def wait_for_timeout(self, ms: float) -> None:
        await asyncio.sleep(ms / 1000)

    def locator(self, _sel):
        return _NoLoc()

    async def expose_function(self, name, _fn) -> None:
        # 真实 Playwright 对同名注册会报错 → 假页面也照此模拟（验证我们的幂等处理）
        if name in self.exposed:
            raise RuntimeError(f"Function {name} has been already registered")
        self.exposed.append(name)

    async def evaluate(self, script, arg=None):
        self.evaluated.append((script, arg))
        return "ok"


def _prov(capture_mode: str = "poll", **kw) -> WebChatProvider:
    cfg = ProviderConfig.model_validate(
        {
            "name": "chatgpt",
            "url": "https://chatgpt.com",
            "models": [{"name": "gpt-5-web"}],
            "selectors": kw.pop("selectors", {"response_container": [".md"], "thinking_container": []}),
            "capture_mode": capture_mode,
            "poll_interval": 0.2,
            "stable_polls": 2,
            "min_wait_before_stable": 0.0,
            "response_timeout": kw.pop("response_timeout", 5.0),
            **kw,
        }
    )
    return WebChatProvider(cfg, _StubBrowser())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_wait_tick_poll_mode_uses_timer():
    """poll 模式：等待原语就是定时等待（与旧行为一致）。"""
    prov = _prov()
    page = _FakePage()
    t0 = time.monotonic()
    await prov._wait_tick(page, 120)
    assert time.monotonic() - t0 >= 0.11


@pytest.mark.asyncio
async def test_wait_tick_observer_wakes_immediately():
    """observer 模式：事件一到就立刻返回（不必等满一个轮询周期）。"""
    prov = _prov(capture_mode="observer")
    prov._obs_wake = asyncio.Event()
    page = _FakePage()

    async def trigger():
        await asyncio.sleep(0.02)
        prov._on_obs_event({"gen": prov._obs_gen})

    asyncio.create_task(trigger())
    t0 = time.monotonic()
    await prov._wait_tick(page, 1000)          # 轮询周期 1s，但应被事件立刻唤醒
    elapsed = time.monotonic() - t0
    assert elapsed < 0.3, f"应被事件唤醒，实际等了 {elapsed:.2f}s"
    assert prov._obs_wake is not None and not prov._obs_wake.is_set(), "唤醒标志应被清掉"


@pytest.mark.asyncio
async def test_wait_tick_observer_falls_back_to_timeout():
    """observer 模式没有事件时，仍按 poll_interval 兜底（结束判定需要周期检查）。"""
    prov = _prov(capture_mode="observer")
    prov._obs_wake = asyncio.Event()
    page = _FakePage()
    t0 = time.monotonic()
    await prov._wait_tick(page, 100)
    assert 0.09 <= time.monotonic() - t0 < 0.35


@pytest.mark.asyncio
async def test_wait_tick_observer_enforces_min_interval():
    """重渲染风暴：事件过密也要限速（observer_min_interval_ms）。"""
    prov = _prov(capture_mode="observer", observer_min_interval_ms=250)
    prov._obs_wake = asyncio.Event()
    page = _FakePage()
    prov._obs_last_tick = time.monotonic()      # 刚提取过
    prov._obs_wake.set()
    t0 = time.monotonic()
    await prov._wait_tick(page, 1000)
    assert time.monotonic() - t0 >= 0.2, "两次提取之间必须满足最小间隔"


def test_stale_generation_events_are_ignored():
    """跨轮污染防护：上一轮（旧世代）的事件必须丢弃。"""
    prov = _prov(capture_mode="observer")
    prov._obs_wake = asyncio.Event()
    prov._obs_gen = 7
    prov._on_obs_event({"gen": 3})              # 旧世代
    assert not prov._obs_wake.is_set(), "旧世代事件不该唤醒本轮"
    prov._on_obs_event({"gen": 7})
    assert prov._obs_wake.is_set(), "当前世代事件必须唤醒"


def test_stop_observer_marks_fallback():
    prov = _prov(capture_mode="observer")
    tl = RequestTimeline(provider="chatgpt")
    prov._tl = tl
    prov._obs_wake = asyncio.Event()
    prov._stop_observer(reason="测试")
    assert prov._obs_wake is None
    assert tl.notes["observer_fallback"] == "1"
    assert tl.notes["capture"] == "observer→poll"


@pytest.mark.asyncio
async def test_start_observer_injects_and_is_idempotent():
    prov = _prov(capture_mode="observer")
    tl = RequestTimeline(provider="chatgpt")
    prov._tl = tl
    page = _FakePage()
    assert await prov._start_observer(page) is True
    js, args = page.evaluated[-1]
    assert "MutationObserver" in js and "characterData" in js
    assert args[0] == [".md"] and isinstance(args[2], int)      # 容器选择器 + 世代号
    gen1 = prov._obs_gen
    await prov._start_observer(page)                            # 再次注入不应报错/不应换号
    assert prov._obs_gen > gen1
    assert page.exposed.count("__aiw2aPush") == 1               # expose 只注册一次
    assert tl.notes["capture"] == "observer"


@pytest.mark.asyncio
async def test_start_observer_failure_falls_back_to_poll():
    """注入失败（页面不可用等）→ 回退轮询，不抛错。"""
    prov = _prov(capture_mode="observer")
    tl = RequestTimeline(provider="chatgpt")
    prov._tl = tl
    page = _FakePage()

    async def boom(*_a, **_kw):
        raise RuntimeError("CSP 拦截")

    page.evaluate = boom  # type: ignore[assignment]
    assert await prov._start_observer(page) is False
    assert prov._obs_wake is None
    assert tl.notes["observer_fallback"] == "1"


@pytest.mark.asyncio
async def test_grace_without_container_falls_back_to_poll(monkeypatch):
    """发了消息但长时间既无事件、又无回复容器 → 判定注入失效 → 回退 poll。"""
    prov = _prov(capture_mode="observer", response_timeout=0.6,
                 observer_grace_seconds=0.2)
    tl = RequestTimeline(provider="chatgpt")
    prov._tl = tl
    page = _FakePage()

    async def no_match(*_a, **_kw):
        return 0

    monkeypatch.setattr(ex, "count_matches", no_match)
    await prov._start_observer(page)
    with pytest.raises(Exception):
        [c async for c in prov._poll_response_dom(page, {".md": 0}, {})]
    assert prov._obs_wake is None, "超过 grace 必须回退轮询"
    assert tl.notes["observer_fallback"] == "1"


@pytest.mark.asyncio
async def test_reset_request_state_must_run_before_start_observer():
    """回归：复位请求级状态**不能**在 _start_observer 之后（会把唤醒器清掉）。

    实测踩过：capture 标成 observer、但一个事件都没到 —— 因为 generate() 里的
    `self._obs_wake = None` 落在 _start_observer 之后，把刚装好的唤醒器清空了。
    """
    prov = _prov(capture_mode="observer")
    page = _FakePage()
    prov._reset_request_state()
    assert await prov._start_observer(page) is True
    assert prov._obs_wake is not None, "启动后必须有唤醒器"
    # 若复位再被调用（错误顺序），唤醒器会被清掉 → 用断言把顺序固定住
    prov._reset_request_state()
    assert prov._obs_wake is None
    assert await prov._start_observer(page) is True     # 正确顺序：复位在前、启动在后
    assert prov._obs_wake is not None


def test_generate_resets_state_before_starting_observer():
    """静态守卫：generate() 里 _reset_request_state() 必须出现在 _start_observer 之前。"""
    from pathlib import Path

    src = Path("src/ai_web2api/providers/webchat.py").read_text(encoding="utf-8")
    body = src[src.index("    async def generate("):]
    body = body[:body.index("    async def _ensure_input")]
    assert "_reset_request_state()" in body and "_start_observer(" in body
    assert body.index("_reset_request_state()") < body.index("_start_observer("), (
        "复位必须早于启动 observer，否则唤醒器会被清掉"
    )


@pytest.mark.asyncio
async def test_silent_observer_failure_reinjects_then_falls_back():
    """启用后**一直没有任何事件**（注入失效/选择器不匹配）→ 先重注入一次，仍无 → 回退 poll。"""
    prov = _prov(capture_mode="observer", observer_grace_seconds=0.05)
    tl = RequestTimeline(provider="chatgpt")
    prov._tl = tl
    page = _FakePage()
    assert await prov._start_observer(page) is True
    prov._obs_started_at = time.monotonic() - 1          # 假装已经过了 grace
    await prov._wait_tick(page, 1)                        # 第一次：重注入
    assert prov._obs_reinjected is True
    assert len(page.evaluated) >= 2, "应重新注入一次"
    assert tl.notes.get("capture") == "observer"
    prov._obs_started_at = time.monotonic() - 1
    await prov._wait_tick(page, 1)                        # 第二次：回退
    assert prov._obs_wake is None
    assert tl.notes["capture"] == "observer→poll"
    assert tl.notes["observer_fallback"] == "1"


@pytest.mark.asyncio
async def test_observer_with_events_never_falls_back():
    """有事件就说明观察器活着 → 绝不能误判回退。"""
    prov = _prov(capture_mode="observer", observer_grace_seconds=0.05)
    tl = RequestTimeline(provider="chatgpt")
    prov._tl = tl
    page = _FakePage()
    await prov._start_observer(page)
    prov._on_obs_event({"gen": prov._obs_gen})            # 收到事件
    prov._obs_started_at = time.monotonic() - 1
    prov._obs_wake.set()
    await prov._wait_tick(page, 10)
    assert prov._obs_wake is not None, "有事件时不该回退"
    assert "observer_fallback" not in tl.notes
