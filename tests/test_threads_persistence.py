"""ThreadManager 走 SQLite 持久化：迁移 / 列表面板 / 消息落库 / 废弃删除。

不依赖浏览器：直接构造 ThreadManager（默认配置），用 stub provider/page 驱动。
运行：.venv/bin/python -m pytest tests/test_threads_persistence.py -v
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ai_web2api.config import ServerConfig
from ai_web2api.core.threads import ThreadManager, ThreadSession

pytestmark = pytest.mark.asyncio  # 本文件含 async 用例（pytest-asyncio strict）


class StubPage:
    def __init__(self, url: str = "https://chat.deepseek.com/a/chat/s/abc123"):
        self.url = url
        self.closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True


class StubProvider:
    name = "deepseek"
    session_url_pattern = r"/a/chat/s/([0-9a-f-]+)"

    class Cfg:
        url = "https://chat.deepseek.com"

    def __init__(self) -> None:
        self.cfg = self.Cfg()


def _server(**kw) -> ServerConfig:
    return ServerConfig(**kw)


async def test_migrate_json_on_init(tmp_path: Path):
    """构造即迁移：旧 threads.json 导入 DB 后删除，列表可见（loaded=false）。"""
    (tmp_path / "deepseek").mkdir()
    (tmp_path / "deepseek" / "threads.json").write_text(
        json.dumps({"t1": {"url": "u1", "model": "deepseek-web", "title": "标题"}}),
        encoding="utf-8",
    )
    tm = ThreadManager(_server(), tmp_path)
    try:
        assert not (tmp_path / "deepseek" / "threads.json").exists()
        items = {t["thread_id"]: t for t in tm.list()}
        assert items["t1"]["loaded"] is False
        assert items["t1"]["first_message"] == "标题"
        assert items["t1"]["model"] == "deepseek-web"
    finally:
        tm.shutdown()


async def test_save_turn_and_get_messages(tmp_path: Path):
    tm = ThreadManager(_server(), tmp_path)
    try:
        await tm.save_turn("t1", "deepseek", "deepseek-web", "你好", "你好呀", "思考中")
        msgs = await tm.get_messages("t1")
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert msgs[0]["content"] == "你好"
        assert msgs[1]["content"] == "你好呀"
        assert msgs[1]["reasoning"] == "思考中"
        # 会话行也建好了（左侧列表可见）
        assert any(t["thread_id"] == "t1" for t in tm.list())
    finally:
        tm.shutdown()


async def test_persist_url_id_survives_reload(tmp_path: Path):
    """persist() 把绑定页的 provider 会话 id 落库；新实例（模拟重启）能读到。"""
    tm = ThreadManager(_server(), tmp_path)
    page = StubPage()
    tm._sessions["t1"] = ThreadSession("t1", StubProvider(), page, "m", first_message="标题")
    try:
        await tm.persist("t1")
    finally:
        tm.shutdown()

    tm2 = ThreadManager(_server(), tmp_path)
    try:
        entry = {t["thread_id"]: t for t in tm2.list()}["t1"]
        assert entry["loaded"] is False
        assert entry["first_message"] == "标题"
    finally:
        tm2.shutdown()


async def test_close_discard_deletes_db_only_thread(tmp_path: Path):
    """DELETE /admin/threads/{id} → 连历史一起删（即使没有活跃页面）。"""
    tm = ThreadManager(_server(), tmp_path)
    try:
        await tm.save_turn("t1", "deepseek", "m", "q", "a")
        assert any(t["thread_id"] == "t1" for t in tm.list())

        assert await tm.close("t1") is True  # DB-only：靠删除持久化条目返回 True
        assert all(t["thread_id"] != "t1" for t in tm.list())
        assert await tm.get_messages("t1") == []
    finally:
        tm.shutdown()


async def test_close_without_discard_keeps_history(tmp_path: Path):
    """TTL 回收（discard=False）只关页面，历史保留。"""
    tm = ThreadManager(_server(), tmp_path)
    page = StubPage()
    tm._sessions["t1"] = ThreadSession("t1", StubProvider(), page, "m")
    try:
        await tm.save_turn("t1", "deepseek", "m", "q", "a")
        assert await tm.close("t1", discard=False) is True
        assert page.closed is True
        assert len(await tm.get_messages("t1")) == 2  # 历史仍在
    finally:
        tm.shutdown()


async def test_persist_disabled_writes_nothing(tmp_path: Path):
    tm = ThreadManager(_server(thread_persist=False), tmp_path)
    try:
        await tm.save_turn("t1", "deepseek", "m", "q", "a")
        assert tm.list() == []  # 未落库
        assert await tm.get_messages("t1") == []
    finally:
        tm.shutdown()


async def test_list_sorted_newest_first(tmp_path: Path):
    """DB 会话按最近活跃倒序：后写的在最前。"""
    tm = ThreadManager(_server(), tmp_path)
    try:
        for tid in ("t1", "t2", "t3"):
            await tm.save_turn(tid, "deepseek", "m", "q", "a")
            await asyncio.sleep(0.005)  # 拉开 updated_at
        assert [t["thread_id"] for t in tm.list()] == ["t3", "t2", "t1"]
    finally:
        tm.shutdown()


async def test_list_active_session_sorts_with_db(tmp_path: Path):
    """活跃会话（内存）与已落库会话混排，最新的最先。"""
    tm = ThreadManager(_server(), tmp_path)
    try:
        await tm.save_turn("old", "deepseek", "m", "q", "a")
        await asyncio.sleep(0.005)
        page = StubPage()
        tm._sessions["new"] = ThreadSession("new", StubProvider(), page, "m")
        assert [t["thread_id"] for t in tm.list()] == ["new", "old"]
    finally:
        tm.shutdown()
