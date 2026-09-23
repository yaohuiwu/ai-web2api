"""闸门（SerialGate）：只为**可用性 + 正确性**——串行、短队列、快速失败、取消不泄漏。

设计见 docs/CONCURRENCY.md。

运行：.venv/bin/python -m pytest tests/test_serial_gate.py -v
"""

from __future__ import annotations

import asyncio

import pytest

from ai_web2api.core.errors import QueueFullError
from ai_web2api.core.queue import SerialGate


async def _hold(gate: SerialGate, log: list, tag: str, hold: float = 0.05):
    await gate.acquire()
    log.append(f"{tag}:start")
    await asyncio.sleep(hold)
    log.append(f"{tag}:end")
    gate.release()


@pytest.mark.asyncio
async def test_serial_fifo():
    """同一 provider 一次只跑一个，且按到达顺序（保证页面不被并发打断）。"""
    gate = SerialGate(max_waiters=10, timeout=5)
    log: list[str] = []
    await gate.acquire()
    t1 = asyncio.create_task(_hold(gate, log, "a"))
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(_hold(gate, log, "b"))
    await asyncio.sleep(0.01)
    t3 = asyncio.create_task(_hold(gate, log, "c"))
    await asyncio.sleep(0.02)
    gate.release()
    await asyncio.gather(t1, t2, t3)
    assert log[:6] == ["a:start", "a:end", "b:start", "b:end", "c:start", "c:end"], log


@pytest.mark.asyncio
async def test_short_queue_fails_fast():
    """队列要短：超出上限立刻 429（不给客户端无限排队）。"""
    gate = SerialGate(max_waiters=2, timeout=5)
    await gate.acquire()
    t1 = asyncio.create_task(gate.acquire())
    t2 = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0.02)
    with pytest.raises(QueueFullError):
        await gate.acquire()                      # 第 3 个 → 直接满
    gate.release()
    await asyncio.gather(t1, t2, return_exceptions=True)


@pytest.mark.asyncio
async def test_queue_timeout_fails_fast():
    gate = SerialGate(max_waiters=5, timeout=0.05)
    await gate.acquire()
    with pytest.raises(QueueFullError):
        await gate.acquire()                      # 0.05s 拿不到 → 429，而不是干等
    assert not gate.has_waiters, "超时后不能留下幽灵等待者"
    gate.release()


@pytest.mark.asyncio
async def test_cancel_releases_and_does_not_deadlock():
    """客户端断流/取消 → 执行权必须还回去（否则后续请求全部被堵死）。"""
    gate = SerialGate(max_waiters=5, timeout=5)
    await gate.acquire()
    t = asyncio.create_task(_hold(gate, [], "x", hold=1))
    await asyncio.sleep(0.02)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    gate.release()
    assert not gate.has_waiters and not gate.busy
    assert await gate.acquire() == 0.0             # 仍可正常获取
    gate.release()


@pytest.mark.asyncio
async def test_max_inflight_option():
    """需要并行不同会话时可放开（默认 1 = 严格串行）。"""
    gate = SerialGate(max_waiters=5, timeout=5, max_inflight=2)
    log: list[str] = []
    await asyncio.gather(_hold(gate, log, "a", 0.05), _hold(gate, log, "b", 0.05))
    assert log[:2] == ["a:start", "b:start"], log


@pytest.mark.asyncio
async def test_run_helpers_and_stats():
    gate = SerialGate(max_waiters=5, timeout=5)
    waits: list[float] = []
    await gate.acquire()

    async def work(x: int) -> int:
        return x * 2

    async def stream():
        yield 1
        yield 2

    async def release_soon():
        await asyncio.sleep(0.05)
        gate.release()

    asyncio.create_task(release_soon())
    assert await gate.run(work, 21, on_queued=waits.append) == 42
    assert waits and waits[0] > 10, waits                 # 等锁时长会回调给时间线
    assert [x async for x in gate.run_iter(stream)] == [1, 2]
    st = gate.stats()
    assert st["max_inflight"] == 1 and st["avg_wait_ms"] >= 0
