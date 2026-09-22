"""会话绑定（thread_id）：复用同一个 Web AI 页面进行多轮对话。

无状态模式每次请求开新 Tab；thread 模式把 Tab 常驻，续用时直接复用页面，
模型靠页面自身历史保持上下文（"页面为准"，只发最后一条 user 消息）。

thread 持久化：会话元数据（provider 会话 id / model / 标题）与消息历史统一落盘到
SQLite（``profiles/threads.db``，见 :mod:`ai_web2api.core.store`）。服务重启后
同 thread_id 请求可 ``goto`` 旧会话 URL 恢复——只要 DeepSeek 不删用户会话，
thread_id 不变就能在上次基础上继续生成。
空闲回收（TTL）与服务关闭保留 DB 条目（页面关了但会话还在，可恢复）；
显式销毁（DELETE / 页面失效 / 超时 / 忙）才删除（含历史消息）。
旧的 ``profiles/<provider>/threads.json`` 启动时迁移进库并删除。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..browser import extractor
from ..core.errors import QueueFullError, ThreadMismatchError
from .store import ThreadStore

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ..config import ServerConfig
    from ..providers.base import BaseProvider

logger = logging.getLogger(__name__)


class ThreadSession:
    """一个绑定会话：复用同一页面。"""

    __slots__ = (
        "thread_id", "provider", "page", "model",
        "lock", "created", "last_used", "created_at", "updated_at", "url_id", "first_message",
    )

    def __init__(
        self,
        thread_id: str,
        provider: BaseProvider,
        page: Page,
        model: str,
        first_message: str = "",
    ) -> None:
        self.thread_id = thread_id
        self.provider = provider
        self.page = page
        self.model = model
        self.first_message = first_message  # 会话第一句 user 消息（左侧列表标题）
        self.lock = asyncio.Lock()  # 同一 thread 串行（页面只有一个输入框）
        self.url_id: str | None = None  # provider 会话 id（DeepSeek: /a/chat/s/<uuid>）
        now = time.monotonic()
        self.created = now
        self.last_used = now
        # 墙钟时间（供列表按时间倒序）；created/last_used 是单调钟，仅用于 TTL
        self.created_at = time.time()
        self.updated_at = self.created_at

    def touch(self) -> None:
        self.last_used = time.monotonic()
        self.updated_at = time.time()

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
            "first_message": self.first_message,
            "created": round(self.created, 1),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "idle_seconds": round(time.monotonic() - self.last_used, 1),
            "page_url": self.page.url if not self.page.is_closed() else "closed",
            "url_id": self.url_id,
            "loaded": True,
        }


class ThreadManager:
    """thread_id → ThreadSession 的注册表 + 生命周期管理。"""

    def __init__(
        self,
        server: ServerConfig,
        profiles_dir: Path,
    ) -> None:
        self._ttl = server.thread_ttl
        self._max = server.max_threads
        self._persist = server.thread_persist
        self._profiles_dir = Path(profiles_dir)
        self._store = ThreadStore(self._profiles_dir / "threads.db")
        if self._persist:
            try:
                migrated = self._store.migrate_from_json(self._profiles_dir)
                if migrated:
                    logger.info("threads.json → SQLite 迁移完成（%d 条）", migrated)
            except Exception:  # noqa: BLE001
                logger.warning("threads.json 迁移失败（继续启动）", exc_info=True)
        self._sessions: dict[str, ThreadSession] = {}
        self._lock = asyncio.Lock()  # 保护"检查上限 + 注册"（并发创建竞态）
        self._cleanup_lock = asyncio.Lock()

    # ---------- 查询 ----------

    def get(self, thread_id: str) -> ThreadSession | None:
        return self._sessions.get(thread_id)

    def list(self) -> list[dict]:
        """内存活跃会话 + DB 持久化条目（重启后左侧列表不空）。

        统一按最近活跃时间（``updated_at``）倒序，**最新在上**；
        DB 条目标记 loaded=false，点击切换后由下次请求触发恢复。
        """
        out = [s.to_dict() for s in self._sessions.values()]
        seen = {d["thread_id"] for d in out}
        try:
            entries = self._store.list_threads()
        except Exception:  # noqa: BLE001  持久化读取失败不能拖垮列表接口
            logger.warning("读取会话列表失败（只返回内存会话）", exc_info=True)
            entries = []
        for entry in entries:
            tid = entry["thread_id"]
            if tid in seen:
                continue
            out.append(
                {
                    "thread_id": tid,
                    "provider": entry.get("provider") or "",
                    "model": entry.get("model"),
                    "first_message": entry.get("title") or "",
                    "created_at": entry.get("created_at"),
                    "updated_at": entry.get("updated_at"),
                    "loaded": False,
                }
            )
            seen.add(tid)
        # 按最近活跃时间倒序（最新在上）；时间戳缺失的排最后
        out.sort(key=lambda d: d.get("updated_at") or d.get("created_at") or 0, reverse=True)
        return out

    def pages_for(self, provider: str) -> list[Page]:
        """该 provider 活跃会话的页面（供实时画面选页；**不创建**任何页面）。"""
        return [
            s.page
            for s in self._sessions.values()
            if getattr(s, "page", None) is not None and getattr(s.provider, "name", None) == provider
        ]

    def active_count(self) -> int:
        return len(self._sessions)

    def page(
        self,
        *,
        q: str | None = None,
        provider: str | None = None,
        limit: int = 0,
        offset: int = 0,
        order: str = "desc",
    ) -> dict:
        """过滤 + 分页 + 排序（供会话独立页）。

        ``limit<=0`` 表示不限制（保持旧调用方行为）。过滤在**合并后的列表**上做：
        内存活跃会话必须参与合并，SQL 单独分页会漏掉它们；当前量级足够。
        匹配：``q`` 大小写不敏感，命中 title/thread_id/provider/model 任一。
        """
        items = self.list()
        if provider:
            items = [d for d in items if (d.get("provider") or "") == provider]
        needle = (q or "").strip().lower()
        if needle:
            def hit(d: dict) -> bool:
                return needle in " ".join(
                    str(d.get(k) or "")
                    for k in ("first_message", "thread_id", "provider", "model")
                ).lower()

            items = [d for d in items if hit(d)]
        if order == "asc":
            items.reverse()  # list() 已按时间倒序
        total = len(items)
        offset = max(0, offset)
        window = items[offset:] if limit <= 0 else items[offset : offset + limit]
        return {
            "threads": window,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + len(window)) < total,
            "active": self.active_count(),
            "max": self.max_threads,
        }

    @property
    def max_threads(self) -> int:
        return self._max

    # ---------- 持久化（SQLite：会话元数据 + 消息历史） ----------

    def _entry(self, thread_id: str) -> dict | None:
        """读取 DB 中的会话行（不存在返回 None）。"""
        return self._store.get_thread(thread_id)

    # ---------- 生命周期 ----------

    async def get_or_create(
        self,
        thread_id: str,
        provider: BaseProvider,
        model: str,
        first_message: str = "",
    ) -> tuple[ThreadSession, str]:
        """取现有会话，或恢复/创建新会话。

        返回 (session, mode)，mode ∈ {"create", "resume"}：
        - "resume"：页面已有完整历史（内存续用，或从磁盘恢复旧会话页），
          驱动只发最后一条 user 消息，不注入历史、不点新对话；
        - "create"：全新会话（打开新页面，点"开启新对话"）。
        创建/恢复时页面打开失败不会注册残留。
        first_message：create 时的第一句 user 消息（作左侧列表标题）；
        已有会话/磁盘恢复时忽略调用方参数（保留原标题）。
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
            entry = self._entry(thread_id) or {}
            session.url_id = entry.get("url_id")
            if restored:
                # DB 恢复：标题用持久化的第一句话（调用方参数忽略）
                session.first_message = entry.get("title", "") or ""
            else:
                session.first_message = first_message or ""
                if self._persist:
                    # 新建会话：先把元数据入库（左侧列表标题）；失败仅告警
                    try:
                        self._store.upsert_thread(
                            thread_id,
                            provider.name,
                            model=model,
                            title=session.first_message or None,
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning("thread %s 元数据入库失败", thread_id, exc_info=True)
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
            entry = self._entry(thread_id)
            if entry:
                if entry["model"] is not None and entry["model"] != model:
                    raise ThreadMismatchError(
                        f'thread "{thread_id}" 持久化会话绑定 model '
                        f"{entry['model']}，无法切换到 {model}",
                    )
                page = await provider.browser.open_page(
                    provider.name,
                    init_scripts=provider.init_scripts(),
                    locale=provider.locale,
                )
                try:
                    restore_url = provider.session_url(entry["url_id"])
                    if restore_url is None:
                        raise RuntimeError("session_url 模板未配置")
                    await page.goto(restore_url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(2000)  # SPA 渲染会话页
                    sel = await extractor.first_match(page, provider.cfg.selectors.input)
                    if sel is not None:
                        logger.info(
                            "thread %s restored from url_id=%s (model=%s)",
                            thread_id, entry["url_id"], model,
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
        cur = self._entry(thread_id) or {}
        if (
            cur.get("url_id") == url_id
            and cur.get("model") == session.model
            and (cur.get("title") or "") == session.first_message
        ):
            return  # 无变化
        try:
            await asyncio.to_thread(
                self._store.upsert_thread,
                thread_id,
                session.provider.name,
                session.model,
                session.first_message or None,
                url_id,
            )
        except Exception:  # noqa: BLE001  持久化失败不应影响请求结果
            logger.warning("thread %s persist 失败", thread_id, exc_info=True)
            return
        logger.info("thread %s persisted url_id=%s (model=%s)", thread_id, url_id, session.model)

    async def save_turn(
        self,
        thread_id: str,
        provider: str,
        model: str,
        user_text: str,
        assistant_text: str,
        reasoning: str | None = None,
        attachments: list | None = None,
    ) -> None:
        """把一轮对话（user + assistant）写入历史。

        在请求正常完成后调用；``attachments`` 为本轮 user 消息的附件（可选，供回放）。
        受 ``server.thread_persist`` 开关控制。
        """
        if not self._persist:
            return

        def _work() -> None:
            self._store.upsert_thread(thread_id, provider, model=model)
            self._store.append_messages(
                thread_id,
                [
                    ("user", user_text or "", None, attachments),
                    ("assistant", assistant_text or "", reasoning),
                ],
            )

        try:
            await asyncio.to_thread(_work)
            session = self._sessions.get(thread_id)
            if session is not None:
                session.touch()  # 活跃时间对齐（列表按最近活跃倒序）
        except Exception:  # noqa: BLE001  历史写入失败不应影响请求结果
            logger.warning("thread %s save_turn 失败", thread_id, exc_info=True)

    async def get_messages(self, thread_id: str) -> list[dict]:
        """读取某会话的历史消息（按时间顺序）。"""
        try:
            return await asyncio.to_thread(self._store.get_messages, thread_id)
        except Exception:  # noqa: BLE001
            logger.warning("thread %s 历史读取失败", thread_id, exc_info=True)
            return []

    def shutdown(self) -> None:
        """关闭 DB 连接（服务退出时调用）。"""
        try:
            self._store.close()
        except Exception:  # noqa: BLE001
            logger.debug("thread store close failed", exc_info=True)

    async def close(self, thread_id: str, discard: bool = True) -> bool:
        """关闭页面并移除（幂等）。返回是否真的关掉了。

        discard=True：会话废弃（DELETE / 页面失效 / 超时 / 忙），
          同时删除磁盘持久化条目——下次同 id 请求开全新会话；
        discard=False：仅回收资源（TTL 空闲回收、服务关闭），
          磁盘条目保留——重启后同 id 请求可恢复旧会话。
        """
        session = self._sessions.pop(thread_id, None)
        closed = False
        if session is not None:
            try:
                await session.page.close()
            except Exception:  # noqa: BLE001
                logger.debug("thread %s page close failed", thread_id)
            closed = True
        if discard and self._persist:
            # 会话废弃：连历史消息一起删（DB 外键级联）；失败仅告警
            try:
                deleted = await asyncio.to_thread(self._store.delete_thread, thread_id)
                closed = closed or deleted
            except Exception:  # noqa: BLE001
                logger.warning("thread %s 历史删除失败", thread_id, exc_info=True)
        logger.info("thread %s closed (active=%d, discard=%s)", thread_id, len(self._sessions), discard)
        return closed

    async def close_provider(self, provider_name: str, discard: bool = False) -> int:
        """关闭某 provider 的全部活跃会话（导入新登录态/重置 context 时用）。返回关闭数量。

        ``discard=False``（默认）：只关页面，保留 DB 历史（下次同 id 可恢复）。
        """
        ids = [tid for tid, s in self._sessions.items() if s.provider.name == provider_name]
        for tid in ids:
            await self.close(tid, discard=discard)
        if ids:
            logger.info("closed %d active thread session(s) for provider %s", len(ids), provider_name)
        return len(ids)

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
