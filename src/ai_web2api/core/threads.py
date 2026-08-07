"""会话绑定（thread_id）：复用同一个 Web AI 页面进行多轮对话。

无状态模式每次请求开新 Tab；thread 模式把 Tab 常驻，续用时直接复用页面，
模型靠页面自身历史保持上下文（"页面为准"，只发最后一条 user 消息）。

thread 持久化：thread 绑定页的 provider 会话 id（如 DeepSeek 的
``/a/chat/s/<uuid>``）落盘到 ``profiles/<provider>/threads.json``。
服务重启后同 thread_id 请求可 ``goto`` 旧会话 URL 恢复——只要 DeepSeek
不删用户会话，thread_id 不变就能在上次基础上继续生成。
空闲回收（TTL）与服务关闭保留磁盘条目（页面关了但会话还在，可恢复）；
显式销毁（DELETE / 页面失效 / 超时 / 忙）才删除。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..browser import extractor
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
        "lock", "created", "last_used", "url_id",
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
        self.url_id: str | None = None  # provider 会话 id（DeepSeek: /a/chat/s/<uuid>）
        now = time.monotonic()
        self.created = now
        self.last_used = now

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def sync_url(self) -> str | None:
        """从当前页面 URL 提取 provider 会话 id（如 DeepSeek /a/chat/s/<uuid>）。"""
        if self.provider.session_url_pattern and not self.page.is_closed():
            m = re.search(self.provider.session_url_pattern, self.page.url)
            if m:
                self.url_id = m.group(1)
        return self.url_id

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "provider": self.provider.name,
            "model": self.model,
            "created": round(self.created, 1),
            "idle_seconds": round(time.monotonic() - self.last_used, 1),
            "page_url": self.page.url if not self.page.is_closed() else "closed",
            "url_id": self.url_id,
        }


class ThreadManager:
    """thread_id → ThreadSession 的注册表 + 生命周期管理。"""

    def __init__(
        self,
        server: ServerConfig,
        registry: ProviderRegistry,
        profiles_dir: Path,
    ) -> None:
        self._ttl = server.thread_ttl
        self._max = server.max_threads
        self._persist = server.thread_persist
        self._profiles_dir = Path(profiles_dir)
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

    # ---------- 持久化（thread_id → provider 会话 URL id） ----------

    def _persist_path(self, provider: str) -> Path:
        return self._profiles_dir / provider / "threads.json"

    def _load_urls(self, provider: str) -> dict[str, dict]:
        """返回 {thread_id: {"url": str, "model": str | None}}（兼容旧纯字符串格式）。"""
        try:
            data = json.loads(self._persist_path(provider).read_text(encoding="utf-8"))
        except Exception:
            return {}
        out: dict[str, dict] = {}
        for k, v in data.items():
            if isinstance(v, str):
                out[k] = {"url": v, "model": None}
            elif isinstance(v, dict) and isinstance(v.get("url"), str):
                out[k] = {"url": v["url"], "model": v.get("model")}
        return out

    def _save_urls(self, provider: str, urls: dict[str, dict]) -> None:
        p = self._persist_path(provider)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(urls, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, p)
        except Exception:  # noqa: BLE001
            logger.exception("thread persist write failed for provider %s", provider)

    def _discard_persisted(self, thread_id: str, provider: BaseProvider) -> None:
        """会话废弃：删除磁盘持久化条目（下次同 id 请求将开全新会话）。"""
        if not self._persist or not provider.session_url_pattern:
            return
        urls = self._load_urls(provider.name)
        if thread_id in urls:
            del urls[thread_id]
            self._save_urls(provider.name, urls)
            logger.info("thread %s removed from persistence (discarded)", thread_id)

    # ---------- 生命周期 ----------

    async def get_or_create(
        self,
        thread_id: str,
        provider: BaseProvider,
        model: str,
    ) -> tuple[ThreadSession, str]:
        """取现有会话，或恢复/创建新会话。

        返回 (session, mode)，mode ∈ {"create", "resume"}：
        - "resume"：页面已有完整历史（内存续用，或从磁盘恢复旧会话页），
          驱动只发最后一条 user 消息，不注入历史、不点新对话；
        - "create"：全新会话（打开新页面，点"开启新对话"）。
        创建/恢复时页面打开失败不会注册残留。
        """
        existing = self._sessions.get(thread_id)
        if existing is not None:
            if existing.provider is not provider or existing.model != model:
                raise ThreadMismatchError(
                    f'thread "{thread_id}" 已绑定 {existing.provider.name}/{existing.model}，'
                    f"无法切换到 {provider.name}/{model}",
                )
            existing.touch()
            return existing, "resume"

        # 打开页面在锁外做（耗时，不阻塞其他查询）；检查上限与注册在锁内（防并发超限）
        page, restored = await self._restore_or_new_page(thread_id, provider, model)
        async with self._lock:
            existing = self._sessions.get(thread_id)
            if existing is not None:
                await page.close()  # 双开竞态：别人已注册，弃用刚打开的页面
                if existing.provider is not provider or existing.model != model:
                    raise ThreadMismatchError(
                        f'thread "{thread_id}" 已绑定 {existing.provider.name}/{existing.model}，'
                        f"无法切换到 {provider.name}/{model}",
                    )
                existing.touch()
                return existing, "resume"
            if len(self._sessions) >= self._max:
                await page.close()
                raise QueueFullError(
                    f"活跃 thread 已达上限（{self._max}），请关闭不用的会话 "
                    "或通过 DELETE /admin/threads/{id} 释放",
                )
            session = ThreadSession(thread_id, provider, page, model)
            session.url_id = self._load_urls(provider.name).get(thread_id, {}).get("url")
            self._sessions[thread_id] = session
            logger.info(
                "thread %s %s (provider=%s, model=%s, active=%d/%d)",
                thread_id, "restored" if restored else "created",
                provider.name, model, len(self._sessions), self._max,
            )
            return session, "resume" if restored else "create"

    async def _restore_or_new_page(
        self,
        thread_id: str,
        provider: BaseProvider,
        model: str,
    ) -> tuple[Page, bool]:
        """尝试从磁盘恢复 thread 的 provider 会话页；失败则开全新页。

        返回 (page, restored)。恢复成功 = resume 语义（页面自带完整历史）。
        """
        if self._persist and provider.session_url_pattern:
            entry = self._load_urls(provider.name).get(thread_id)
            if entry:
                if entry["model"] is not None and entry["model"] != model:
                    raise ThreadMismatchError(
                        f'thread "{thread_id}" 持久化会话绑定 model '
                        f"{entry['model']}，无法切换到 {model}",
                    )
                page = await provider.browser.open_page(provider.name)
                try:
                    restore_url = f"{provider.cfg.url.rstrip('/')}/a/chat/s/{entry['url']}"
                    await page.goto(restore_url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(2000)  # SPA 渲染会话页
                    sel = await extractor.first_match(page, provider.cfg.selectors.input)
                    if sel is not None:
                        logger.info(
                            "thread %s restored from url_id=%s (model=%s)",
                            thread_id, entry["url"], model,
                        )
                        return page, True
                    logger.warning(
                        "thread %s restore: page not chat-ready (url=%s), fallback to new page",
                        thread_id, page.url,
                    )
                except Exception:
                    logger.warning(
                        "thread %s restore failed, fallback to new page", thread_id,
                        exc_info=True,
                    )
                try:
                    await page.close()
                except Exception:  # noqa: BLE001
                    pass
        page = await provider.open_chat_page()
        return page, False

    async def persist(self, thread_id: str) -> None:
        """把绑定页的 provider 会话 id（如 /a/chat/s/<uuid>）写入磁盘。

        在每次请求正常完成后调用；供重启后 goto 恢复。无会话 id（如新会话
        尚未生成 URL）或 provider 不支持时静默跳过。
        """
        if not self._persist:
            return
        session = self._sessions.get(thread_id)
        if session is None or not session.provider.session_url_pattern:
            return
        url_id = session.sync_url()
        if not url_id:
            return
        urls = self._load_urls(session.provider.name)
        cur = urls.get(thread_id)
        if cur and cur["url"] == url_id and cur.get("model") == session.model:
            return  # 无变化
        urls[thread_id] = {"url": url_id, "model": session.model}
        self._save_urls(session.provider.name, urls)
        logger.info("thread %s persisted url_id=%s (model=%s)", thread_id, url_id, session.model)

    async def close(self, thread_id: str, discard: bool = True) -> bool:
        """关闭页面并移除（幂等）。返回是否真的关掉了。

        discard=True：会话废弃（DELETE / 页面失效 / 超时 / 忙），
          同时删除磁盘持久化条目——下次同 id 请求开全新会话；
        discard=False：仅回收资源（TTL 空闲回收、服务关闭），
          磁盘条目保留——重启后同 id 请求可恢复旧会话。
        """
        session = self._sessions.pop(thread_id, None)
        if session is None:
            return False
        try:
            await session.page.close()
        except Exception:  # noqa: BLE001
            logger.debug("thread %s page close failed", thread_id)
        if discard:
            self._discard_persisted(thread_id, session.provider)
        logger.info("thread %s closed (active=%d, discard=%s)", thread_id, len(self._sessions), discard)
        return True

    async def cleanup(self) -> int:
        """回收空闲超过 TTL 的会话（仅关页面，保留持久化条目可恢复）。返回回收数量。"""
        if not self._sessions:
            return 0
        now = time.monotonic()
        stale = [
            tid for tid, s in self._sessions.items()
            if now - s.last_used > self._ttl
        ]
        for tid in stale:
            await self.close(tid, discard=False)
        if stale:
            logger.info("thread cleanup: recycled %d/%d idle sessions", len(stale), len(self._sessions) + len(stale))
        return len(stale)

    async def close_all(self) -> None:
        """服务关闭时清理（幂等）：关页面但保留持久化条目（重启可恢复）。"""
        for tid in list(self._sessions):
            await self.close(tid, discard=False)
