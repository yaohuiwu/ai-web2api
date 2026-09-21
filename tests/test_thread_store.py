"""ThreadStore（SQLite 会话历史）单元测试。

运行：.venv/bin/python -m pytest tests/test_thread_store.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

from ai_web2api.core.store import ThreadStore


def _store(tmp_path: Path) -> ThreadStore:
    return ThreadStore(tmp_path / "threads.db")


def test_upsert_get_list(tmp_path: Path):
    st = _store(tmp_path)
    st.upsert_thread("t1", "deepseek", model="deepseek-web", title="你好")
    st.upsert_thread("t2", "deepseek", model="deepseek-web", title="再见")

    got = st.get_thread("t1")
    assert got is not None
    assert got["provider"] == "deepseek"
    assert got["model"] == "deepseek-web"
    assert got["title"] == "你好"

    ids = [t["thread_id"] for t in st.list_threads()]
    assert set(ids) == {"t1", "t2"}
    # provider 过滤
    assert [t["thread_id"] for t in st.list_threads("deepseek")] == ids
    assert st.list_threads("other") == []


def test_upsert_preserves_fields_when_none(tmp_path: Path):
    """COALESCE 语义：不传的字段不能被清空（避免 save_turn 把 title 抹掉）。"""
    st = _store(tmp_path)
    st.upsert_thread("t1", "deepseek", model="m", title="标题", url_id="abc")
    st.upsert_thread("t1", "deepseek")  # 只确认会话存在
    got = st.get_thread("t1")
    assert got is not None
    assert got["title"] == "标题"
    assert got["model"] == "m"
    assert got["url_id"] == "abc"


def test_set_url_id_keeps_provider(tmp_path: Path):
    st = _store(tmp_path)
    st.upsert_thread("t1", "deepseek", model="m", title="标题")
    assert st.set_url_id("t1", "uuid-1", model="deepseek-web") is True
    got = st.get_thread("t1")
    assert got is not None
    assert got["provider"] == "deepseek"  # 不能被清空
    assert got["url_id"] == "uuid-1"
    assert got["model"] == "deepseek-web"
    assert got["title"] == "标题"  # 不传 title → 保留
    # 不存在的行不命中
    assert st.set_url_id("nope", "uuid-2") is False


def test_messages_order_and_reasoning(tmp_path: Path):
    st = _store(tmp_path)
    st.upsert_thread("t1", "deepseek", title="标题")
    st.append_messages("t1", [("user", "问", None)])
    st.append_messages("t1", [("assistant", "答", "想了一下")])

    msgs = st.get_messages("t1")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "问"
    assert msgs[0]["reasoning"] is None
    assert msgs[1]["reasoning"] == "想了一下"


def test_delete_thread_cascades_messages(tmp_path: Path):
    st = _store(tmp_path)
    st.upsert_thread("t1", "deepseek", title="标题")
    st.append_messages("t1", [("user", "问", None), ("assistant", "答", None)])
    assert len(st.get_messages("t1")) == 2

    assert st.delete_thread("t1") is True
    assert st.get_thread("t1") is None
    assert st.get_messages("t1") == []  # 级联删除
    assert st.delete_thread("t1") is False


def test_migrate_from_json_imports_and_deletes(tmp_path: Path):
    profiles = tmp_path / "profiles"
    (profiles / "deepseek").mkdir(parents=True)
    (profiles / "deepseek" / "threads.json").write_text(
        json.dumps(
            {
                "old-str": "url-a",  # 最老格式：thread_id → url 字符串
                "old-dict": {"url": "url-b", "model": "deepseek-web", "title": "标题B"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    st = ThreadStore(profiles / "threads.db")
    imported = st.migrate_from_json(profiles)
    assert imported == 2

    a = st.get_thread("old-str")
    assert a is not None and a["url_id"] == "url-a" and a["provider"] == "deepseek"
    b = st.get_thread("old-dict")
    assert b is not None and b["url_id"] == "url-b" and b["title"] == "标题B"
    # 迁移后原 json 被删除
    assert not (profiles / "deepseek" / "threads.json").exists()
    # 幂等：再跑一次无文件、无新增
    assert st.migrate_from_json(profiles) == 0


def test_migrate_does_not_overwrite_existing(tmp_path: Path):
    profiles = tmp_path / "profiles"
    (profiles / "deepseek").mkdir(parents=True)
    (profiles / "deepseek" / "threads.json").write_text(
        json.dumps({"t1": "old-url"}), encoding="utf-8"
    )
    st = ThreadStore(profiles / "threads.db")
    st.upsert_thread("t1", "deepseek", title="db-标题", url_id="new-url")

    assert st.migrate_from_json(profiles) == 0  # DO NOTHING
    got = st.get_thread("t1")
    assert got is not None
    assert got["url_id"] == "new-url"
    assert got["title"] == "db-标题"
