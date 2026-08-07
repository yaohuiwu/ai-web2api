"""会话绑定（thread_id）：复用同一个 Web AI 页面进行多轮对话。

无状态模式每次请求开新 Tab；thread 模式把 Tab 常驻，续用时直接复用页面，
模型靠页面自身历史保持上下文（"页面为准"，只发最后一条 user 消息）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ..core.errors import QueueFullError, ThreadMismatchError

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ..config import ServerConfig
    from ..providers.base import BaseProvider
    from ..providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class ThreadSession:
    """一个绑定会话：复用同一页面。"""

    __slots__ = (
        "thread_id", "provider", "page", "model",
        "lock", "created", "last_used",
    )

    def __init__(
        self,
        thread_id: str,
        provider: BaseProvider,
        page: Page,
        model: str,
    ) -> None:
        self.thread_id = thread_id
        self.provider = provider
        self.page = page
        self.model = model
        self.lock = asyncio.Lock()  # 同一 thread 串行（页面只有一个输入框）
        now = time.monotonic()
        self.created = now
        self.last_used = now

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "provider": self.provider.name,
            "model": self.model,
            "created": round(self.created, 1),
            "idle_seconds": round(time.monotonic() - self.last_used, 1),
            "page_url": self.page.url if not self.page.is_closed() else "closed",
        }


class ThreadManager:
    """thread_id → ThreadSession 的注册表 + 生命周期管理。"""

    def __init__(self, server: ServerConfig, registry: ProviderRegistry) -> None:
        self._ttl = server.thread_ttl
        self._max = server.max_threads
        self._registry = registry
        self._sessions: dict[str, ThreadSession] = {}
        self._lock = asyncio.Lock()  # 保护"检查上限 + 注册"（并发创建竞态）
        self._cleanup_lock = asyncio.Lock()

    # ---------- 查询 ----------

    def get(self, thread_id: str) -> ThreadSession | None:
        return self._sessions.get(thread_id)

    def list(self) -> list[dict]:
        return [s.to_dict() for s in self._sessions.values()]

    def active_count(self) -> int:
        return len(self._sessions)

    @property
    def max_threads(self) -> int:
        return self._max

    # ---------- 生命周期 ----------

    async def get_or_create(
        self,
        thread_id: str,
        provider: BaseProvider,
        model: str,
    ) -> tuple[ThreadSession, bool]:
        """取现有会话，或创建新会话（打开新页面）。

        返回 (session, is_new)。创建时页面打开失败不会注册残留。
        """
        existing = self._sessions.get(thread_id)
        if existing is not None:
            if existing.provider is not provider or existing.model != model:
                raise ThreadMismatchError(
                    f'thread "{thread_id}" 已绑定 {existing.provider.name}/{existing.model}，'
                    f"无法切换到 {provider.name}/{model}",
                )
            existing.touch()
            return existing, False

        # 打开页面在锁外做（耗时，不阻塞其他查询）；检查上限与注册在锁内（防并发超限）
        page = await provider.open_chat_page()
        async with self._lock:
            existing = self._sessions.get(thread_id)
            if existing is not None:
                await page.close()  # 双开竞态：别人已注册，弃用刚开的页面
                if existing.provider is not provider or existing.model != model:
                    raise ThreadMismatchError(
                        f'thread "{thread_id}" 已绑定 {existing.provider.name}/{existing.model}，'
                        f"无法切换到 {provider.name}/{model}",
                    )
                existing.touch()
                return existing, False
            if len(self._sessions) >= self._max:
                await page.close()
                raise QueueFullError(
                    f"活跃 thread 已达上限（{self._max}），请关闭不用的会话 "
                    "或通过 DELETE /admin/threads/{id} 释放",
                )
            session = ThreadSession(thread_id, provider, page, model)
            self._sessions[thread_id] = session
            logger.info(
                "thread %s created (provider=%s, model=%s, active=%d/%d)",
                thread_id, provider.name, model, len(self._sessions), self._max,
            )
            return session, True

    async def close(self, thread_id: str) -> bool:
        """关闭页面并移除（幂等）。返回是否真的关掉了。"""
        session = self._sessions.pop(thread_id, None)
        if session is None:
            return False
        try:
            await session.page.close()
        except Exception:  # noqa: BLE001
            logger.debug("thread %s page close failed", thread_id)
        logger.info("thread %s closed (active=%d)", thread_id, len(self._sessions))
        return True

    async def cleanup(self) -> int:
        """回收空闲超过 TTL 的会话。返回回收数量。"""
        if not self._sessions:
            return 0
        now = time.monotonic()
        stale = [
            tid for tid, s in self._sessions.items()
            if now - s.last_used > self._ttl
        ]
        for tid in stale:
            await self.close(tid)
        if stale:
            logger.info("thread cleanup: recycled %d/%d idle sessions", len(stale), len(self._sessions) + len(stale))
        return len(stale)

    async def close_all(self) -> None:
        """服务关闭时清理（幂等）。"""
        for tid in list(self._sessions):
            await self.close(tid)
