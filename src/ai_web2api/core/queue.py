"""每 Provider 一个串行门：同一 Web AI 一次只能聊一个会话，请求排队执行。"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Awaitable, Callable, TypeVar

from .errors import QueueFullError

T = TypeVar("T")


class SerialGate:
    """同一时刻只允许一个请求执行。

    - 等待队列长度超过 ``max_waiters`` → 429 QueueFullError
    - 等待超过 ``timeout`` 秒仍未获得执行权 → 429 QueueFullError
    """

    def __init__(self, max_waiters: int = 10, timeout: float = 60.0):
        self._sem = asyncio.Semaphore(1)
        self._max_waiters = max_waiters
        self._timeout = timeout
        self._waiting = 0
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        """是否有请求正在执行（供实时画面降帧判断）。"""
        return self._sem.locked()

    async def _acquire(self) -> None:
        async with self._lock:
            if self._waiting >= self._max_waiters:
                raise QueueFullError(f"provider queue full ({self._max_waiters} waiters)")
            self._waiting += 1
        try:
            try:
                await asyncio.wait_for(self._sem.acquire(), timeout=self._timeout)
            except asyncio.TimeoutError:
                raise QueueFullError(f"provider queue timeout after {self._timeout}s") from None
        except BaseException:
            async with self._lock:
                self._waiting -= 1
            raise

    async def run(self, coro_fn: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        """串行执行一个协程函数。"""
        await self._acquire()
        try:
            return await coro_fn(*args, **kwargs)
        finally:
            self._sem.release()
            async with self._lock:
                self._waiting -= 1

    async def run_iter(self, agen_fn: Callable[..., AsyncIterator[T]], *args, **kwargs) -> AsyncIterator[T]:
        """串行执行一个异步生成器函数，逐个产出元素。"""
        await self._acquire()
        try:
            async for item in agen_fn(*args, **kwargs):
                yield item
        finally:
            self._sem.release()
            async with self._lock:
                self._waiting -= 1
