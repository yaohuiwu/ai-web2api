"""会话历史持久化（SQLite）。

替代原先的 ``profiles/<provider>/threads.json``：会话元数据（provider/model/
title/会话 url_id）与消息内容（user/assistant + 思考）统一落盘到单个 SQLite
文件（默认 ``profiles/threads.db``，位于 ``profiles_dir`` 下，Docker 卷已覆盖）。

同步实现（连接 + ``threading.Lock`` 串行化），便于单测；异步调用方
（:class:`~ai_web2api.core.threads.ThreadManager`）用 ``asyncio.to_thread``
包装，避免阻塞事件循环。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    thread_id  TEXT PRIMARY KEY,
    provider   TEXT NOT NULL,
    model      TEXT,
    title      TEXT,
    url_id     TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    reasoning  TEXT,
    attachments TEXT,
    widgets TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, id);
CREATE TABLE IF NOT EXISTS metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    provider   TEXT NOT NULL,
    model      TEXT,
    thread_id  TEXT DEFAULT 'stateless',
    ok         INTEGER NOT NULL DEFAULT 1,
    total      REAL,
    ttft       REAL,
    settle_lag REAL,
    tail       REAL,
    finalize   TEXT,
    error      TEXT,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS ix_metrics_provider ON metrics(provider, created_at);
"""


class ThreadStore:
    """threads / messages 两张表的同步 CRUD。

    - ``threads.thread_id`` 主键；``messages`` 外键级联删除。
    - 单连接 + 锁：写操作串行，读也走同一把锁（sqlite 连接非线程安全，
      统一串行最省心）。
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()
        logger.info("thread store ready: %s", self._path)

    def _migrate(self) -> None:
        """轻量迁移：给旧库的 messages 表补 attachments 列。"""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(messages)")}
        if "attachments" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN attachments TEXT")
            logger.info("thread store migrated: messages.attachments added")
        if "widgets" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN widgets TEXT")
            logger.info("thread store migrated: messages.widgets added")

    # ---------- 生命周期 ----------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- threads ----------

    def upsert_thread(
        self,
        thread_id: str,
        provider: str,
        model: str | None = None,
        title: str | None = None,
        url_id: str | None = None,
    ) -> None:
        """建/更新会话行。

        冲突时用 ``COALESCE``：只有传入非 ``None`` 才覆盖对应字段，
        因此 ``upsert_thread(tid, provider)`` 不会清空已有的 title/url_id。
        """
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO threads
                    (thread_id, provider, model, title, url_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    provider   = excluded.provider,
                    model      = COALESCE(excluded.model, threads.model),
                    title      = COALESCE(excluded.title, threads.title),
                    url_id     = COALESCE(excluded.url_id, threads.url_id),
                    updated_at = excluded.updated_at
                """,
                (thread_id, provider, model, title, url_id, now, now),
            )
            self._conn.commit()

    def get_thread(self, thread_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_threads(self, provider: str | None = None) -> list[dict]:
        """按 ``updated_at`` 倒序返回会话（供左侧列表）。"""
        with self._lock:
            if provider:
                rows = self._conn.execute(
                    "SELECT * FROM threads WHERE provider = ? ORDER BY updated_at DESC",
                    (provider,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM threads ORDER BY updated_at DESC"
                ).fetchall()
        return [dict(r) for r in rows]

    def delete_thread(self, thread_id: str) -> bool:
        """删除会话（外键级联删除其消息）。返回是否删到了行。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM threads WHERE thread_id = ?", (thread_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- messages ----------

    def append_messages(
        self,
        thread_id: str,
        entries: list[tuple],
    ) -> int:
        """追加消息。``entries`` 每项为 ``(role, content, reasoning)`` 或
        ``(role, content, reasoning, attachments)``；``attachments`` 为可 JSON 序列化的列表。

        ``threads`` 行须已存在（``upsert_thread``），否则外键约束报错。
        """
        if not entries:
            return 0
        now = time.time()
        rows = []
        for e in entries:
            role, content, reasoning = e[0], e[1], e[2]
            atts = e[3] if len(e) > 3 else None
            wgs = e[4] if len(e) > 4 else None
            rows.append(
                (
                    thread_id,
                    role,
                    content or "",
                    reasoning,
                    json.dumps(atts, ensure_ascii=False) if atts else None,
                    json.dumps(wgs, ensure_ascii=False) if wgs else None,
                    now,
                )
            )
        with self._lock:
            self._conn.executemany(
                "INSERT INTO messages (thread_id, role, content, reasoning, attachments, widgets, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
        return len(rows)

    def get_messages(self, thread_id: str) -> list[dict]:
        """按写入顺序（自增 id 升序）返回某会话的全部消息（含附件）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, reasoning, attachments, widgets, created_at FROM messages "
                "WHERE thread_id = ? ORDER BY id",
                (thread_id,),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            for key in ("attachments", "widgets"):
                if d.get(key):
                    try:
                        d[key] = json.loads(d[key])
                    except Exception:  # noqa: BLE001
                        d[key] = None
                else:
                    d[key] = None
            out.append(d)
        return out

    # ---------- metrics ----------

    def record_metric(
        self,
        provider: str,
        ok: bool,
        total: float | None = None,
        ttft: float | None = None,
        settle_lag: float | None = None,
        tail: float | None = None,
        finalize: str = "",
        error: str = "",
        model: str = "",
        thread_id: str = "stateless",
    ) -> None:
        """写一条请求指标（同步，内部已串行化）。"""
        with self._lock:
            self._conn.execute(
                """INSERT INTO metrics
                    (provider, model, thread_id, ok, total, ttft, settle_lag, tail, finalize, error)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (provider, model, thread_id, 1 if ok else 0,
                 total, ttft, settle_lag, tail, finalize, error or None),
            )
            self._conn.commit()

    def aggregate_metrics(
        self, window_hours: float = 24
    ) -> list[dict]:
        """滚动窗口内按 provider 聚合。

        返回列表每项：
        ``name/n/ok_n/success_rate/avg_total/p50_total/p95_total/
        avg_ttft/finalize_counts/last_ok/last_error``。
        ``ranking`` 按成功率降序、同率按平均耗时升序。
        """
        cutoff = time.time() - window_hours * 3600
        with self._lock:
            rows = self._conn.execute(
                """SELECT provider, ok, total, ttft, finalize, error, created_at
                     FROM metrics
                    WHERE created_at >= ?
                    ORDER BY created_at DESC""",
                (cutoff,),
            ).fetchall()
        if not rows:
            return []
        # 按 provider 分组
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r[0], []).append(
                {"ok": r[1], "total": r[2], "ttft": r[3],
                 "finalize": r[4], "error": r[5]}
            )
        out = []
        for prov, items in groups.items():
            n = len(items)
            ok_n = sum(1 for x in items if x["ok"])
            rates = [x["total"] for x in items if x["total"] is not None]
            ttfts = [x["ttft"] for x in items if x["ttft"] is not None]
            # finalize 计数
            fc: dict[str, int] = {}
            for x in items:
                if x["finalize"]:
                    fc[x["finalize"]] = fc.get(x["finalize"], 0) + 1
            # 最近一条（created_at DESC 已排序，取第一个）
            recent = items[-1] if items else {}
            success_rate = round(100.0 * ok_n / n, 1) if n else 0.0

            def pct(vals: list[float], p: float) -> float | None:
                if not vals:
                    return None
                s = sorted(vals)
                k = (len(s) - 1) * p
                lo = int(k)
                hi = lo + 1
                if hi >= len(s):
                    return round(s[-1], 2)
                return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 2)

            out.append(
                {
                    "name": prov,
                    "n": n,
                    "ok_n": ok_n,
                    "success_rate": success_rate,
                    "avg_total": round(sum(rates) / len(rates), 2) if rates else None,
                    "p50_total": pct(rates, 0.5),
                    "p95_total": pct(rates, 0.95),
                    "avg_ttft": round(sum(ttfts) / len(ttfts), 2) if ttfts else None,
                    "finalize_counts": fc,
                    "last_ok": recent.get("ok", 0) == 1,
                    "last_error": recent.get("error"),
                }
            )
        out.sort(key=lambda x: (-x["success_rate"], x["avg_total"] or 9e9))
        ranking = [x["name"] for x in out]
        out.append({"ranking": ranking})  # 尾元素携带排名
        return out

    def clear_metrics_older_than(self, days: int = 7) -> int:
        """删掉早于 `days` 天的指标，返回删除行数。"""
        cutoff = time.time() - days * 86400
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM metrics WHERE created_at < ?", (cutoff,)
            )
            self._conn.commit()
        return cur.rowcount

    # ---------- 迁移 ----------

    def migrate_from_json(self, profiles_dir: str | Path) -> int:
        """把 ``profiles/<provider>/threads.json`` 导入 DB 并删除原文件。

        旧格式兼容两种：
        - ``{"tid": "<url>"}``（最老）
        - ``{"tid": {"url": ..., "model": ..., "title": ...}}``（当前）

        幂等：已存在的 ``thread_id`` 用 ``DO NOTHING`` 跳过（不覆盖库内新数据）。
        导入成功后删除 json 文件。返回实际新增的行数。
        """
        profiles_dir = Path(profiles_dir)
        if not profiles_dir.exists():
            return 0
        imported = 0
        for json_file in sorted(profiles_dir.glob("*/threads.json")):
            provider = json_file.parent.name
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                logger.warning("threads.json 解析失败，跳过：%s", json_file, exc_info=True)
                continue
            if not isinstance(data, dict):
                continue
            now = time.time()
            with self._lock:
                for tid, v in data.items():
                    if isinstance(v, str):
                        url_id, model, title = v, None, ""
                    elif isinstance(v, dict) and isinstance(v.get("url"), str):
                        url_id = v["url"]
                        model = v.get("model")
                        title = v.get("title", "") or ""
                    else:
                        continue
                    cur = self._conn.execute(
                        """
                        INSERT INTO threads
                            (thread_id, provider, model, title, url_id, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(thread_id) DO NOTHING
                        """,
                        (tid, provider, model, title, url_id, now, now),
                    )
                    imported += cur.rowcount if cur.rowcount > 0 else 0
                self._conn.commit()
            try:
                json_file.unlink()
                logger.info("迁移 %s → DB 并删除（%d 条）", json_file, len(data))
            except Exception:  # noqa: BLE001
                logger.warning("迁移后删除 %s 失败", json_file, exc_info=True)
        return imported
