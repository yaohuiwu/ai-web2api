"""每 Provider 一个串行闸门：**保证可用性与正确性**，不做优先级。

一个 Provider 背后是一个真实浏览器页面：同一时刻只能有一个请求在打字/抓取，
否则会互相打断（输入框被覆盖、回答串台、错误串到别的会话）。因此：

1. **串行**：同时最多 ``max_inflight``（默认 1）个请求在跑；
2. **队列要短**：等待上限 ``max_waiters``（默认 3）+ 超时 ``timeout``（默认 30s）→ 立刻 **429**，
   让客户端自己重试（排队干等对谁都没好处）；
3. **取消必须释放**：客户端断流 → 执行权立刻还回，绝不留死锁；
4. **可观测**：``stats()`` 供 ``/admin/status``；请求时间线里记 ``queued_ms``。

实现刻意保持"直白"：单线程事件循环里用**显式 FIFO + 计数器**（临界区无 await，所以不需要锁），
比 Semaphore 更容易看清"谁在跑、谁在等、取消时怎么还回去"。详见 docs/CONCURRENCY.md。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import AsyncIterator, Awaitable, Callable, TypeVar

from .errors import QueueFullError

T = TypeVar("T")


class SerialGate:
    """同一 Provider 的请求闸门（串行 + 短队列 + 快速失败）。"""

    def __init__(self, max_waiters: int = 3, timeout: float = 30.0, *, max_inflight: int = 1):
        self._max_inflight = max(1, int(max_inflight))
        self._max_waiters = max(0, int(max_waiters))
        self._timeout = float(timeout)
        self._inflight = 0                              # 正在跑的请求数
        self._q: deque[asyncio.Future] = deque()        # 排队等待者（FIFO）
        self._waited_ms: deque[float] = deque(maxlen=200)

    # ---------- 只读状态（供实时画面降帧 / 状态面板） ----------

    @property
    def busy(self) -> bool:
        """是否有请求正在执行。"""
        return self._inflight > 0

    @property
    def has_waiters(self) -> bool:
        """是否有人在排队（说明这个 provider 正处于压力中）。"""
        return bool(self._q)

    def stats(self) -> dict:
        waited = list(self._waited_ms)
        return {
            "running": self._inflight,
            "max_inflight": self._max_inflight,
            "waiting": len(self._q),
            "max_waiters": self._max_waiters,
            "avg_wait_ms": round(sum(waited) / len(waited), 1) if waited else 0.0,
            "recent_waits_ms": [round(w, 1) for w in waited[-5:]],
        }

    # ---------- 获取 / 释放 ----------

    async def acquire(self) -> float:
        """获取执行权；返回等待毫秒。队列满/等待超时 → ``QueueFullError``。"""
        t0 = time.monotonic()
        # 临界区（无 await）：直接开跑 or 入队 or 快速失败
        if self._inflight < self._max_inflight and not self._q:
            self._inflight += 1
            return 0.0
        if len(self._q) >= self._max_waiters:
            raise QueueFullError(f"provider queue full ({self._max_waiters} waiters)")

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._q.append(fut)
        try:
            await asyncio.wait_for(fut, timeout=self._timeout)
        except asyncio.TimeoutError:
            self._cancel_waiter(fut)
            raise QueueFullError(
                f"provider queue timeout after {self._timeout:g}s"
            ) from None
        except BaseException:
            # 取消（客户端断流/上游超时）：若已获授权 → 立刻还回去，避免执行权泄漏（死锁）
            granted = fut.done() and not fut.cancelled()
            self._cancel_waiter(fut)
            if granted:
                self.release()
            raise

        waited = (time.monotonic() - t0) * 1000
        if waited > 1:                                  # 真排过队才记（0ms 没意义）
            self._waited_ms.append(waited)
        return waited

    def _cancel_waiter(self, fut: asyncio.Future) -> None:
        """把等待者从队列里摘掉（幂等；临界区无 await）。"""
        if not fut.done():
            fut.cancel()
        try:
            self._q.remove(fut)
        except ValueError:
            pass

    def release(self) -> None:
        """释放执行权并唤醒下一位（FIFO）。"""
        self._inflight = max(0, self._inflight - 1)
        while self._q and self._inflight < self._max_inflight:
            fut = self._q.popleft()
            if fut.done() or fut.cancelled():           # 已超时/取消的等待者：跳过
                continue
            self._inflight += 1
            fut.set_result(None)
            return

    # ---------- 便捷包装（路由用） ----------

    async def run(
        self,
        coro_fn: Callable[..., Awaitable[T]],
        *args,
        on_queued: Callable[[float], None] | None = None,
        **kwargs,
    ) -> T:
        """串行执行一个协程函数。"""
        waited = await self.acquire()
        if on_queued is not None and waited:
            on_queued(waited)
        try:
            return await coro_fn(*args, **kwargs)
        finally:
            self.release()

    async def run_iter(
        self,
        agen_fn: Callable[..., AsyncIterator[T]],
        *args,
        on_queued: Callable[[float], None] | None = None,
        **kwargs,
    ) -> AsyncIterator[T]:
        """串行执行一个异步生成器函数（整个流的生命周期内持有执行权）。"""
        waited = await self.acquire()
        if on_queued is not None and waited:
            on_queued(waited)
        try:
            async for item in agen_fn(*args, **kwargs):
                yield item
        finally:
            self.release()
