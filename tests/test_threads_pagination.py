"""会话列表过滤/分页/排序（ThreadManager.page + /admin/threads 查询参数）。

运行：.venv/bin/python -m pytest tests/test_threads_pagination.py -v
"""

from __future__ import annotations

from pathlib import Path

from ai_web2api.config import ServerConfig
from ai_web2api.core.store import ThreadStore
from ai_web2api.core.threads import ThreadManager


def _server() -> ServerConfig:
    return ServerConfig(thread_persist=True)


def _seed(tmp_path: Path, rows: list[tuple[str, str, str, float]]) -> ThreadManager:
    """rows: [(thread_id, provider, title, updated_at)]"""
    store = ThreadStore(tmp_path / "threads.db")
    for tid, provider, title, ts in rows:
        store.upsert_thread(tid, provider, model=f"{provider}-web", title=title)
        store._conn.execute("UPDATE threads SET updated_at = ? WHERE thread_id = ?", (ts, tid))
    store._conn.commit()
    store.close()
    return ThreadManager(_server(), tmp_path)


ROWS = [
    ("t-old", "deepseek", "介绍西安城墙", 1000.0),
    ("t-mid", "chatgpt", "北京天气怎么样", 2000.0),
    ("t-new", "chatgpt", "生成一张图片", 3000.0),
]


def test_default_order_is_desc_and_unlimited(tmp_path: Path):
    tm = _seed(tmp_path, ROWS)
    page = tm.page()
    assert [t["thread_id"] for t in page["threads"]] == ["t-new", "t-mid", "t-old"]
    assert page["total"] == 3 and page["has_more"] is False and page["limit"] == 0


def test_asc_order(tmp_path: Path):
    tm = _seed(tmp_path, ROWS)
    assert [t["thread_id"] for t in tm.page(order="asc")["threads"]] == ["t-old", "t-mid", "t-new"]


def test_pagination_has_more(tmp_path: Path):
    tm = _seed(tmp_path, ROWS)
    p1 = tm.page(limit=2, offset=0)
    assert [t["thread_id"] for t in p1["threads"]] == ["t-new", "t-mid"]
    assert p1["has_more"] is True
    p2 = tm.page(limit=2, offset=2)
    assert [t["thread_id"] for t in p2["threads"]] == ["t-old"]
    assert p2["has_more"] is False
    # 越界返回空页
    assert tm.page(limit=2, offset=99)["threads"] == []


def test_search_matches_title_id_provider_model(tmp_path: Path):
    tm = _seed(tmp_path, ROWS)
    assert [t["thread_id"] for t in tm.page(q="天气")["threads"]] == ["t-mid"]
    assert [t["thread_id"] for t in tm.page(q="t-old")["threads"]] == ["t-old"]
    assert sorted(t["thread_id"] for t in tm.page(q="chatgpt")["threads"]) == ["t-mid", "t-new"]
    assert len(tm.page(q="deepseek-web")["threads"]) == 1
    assert tm.page(q="不存在的关键词")["threads"] == []
    # 大小写不敏感
    assert len(tm.page(q="CHATGPT")["threads"]) == 2


def test_filter_by_provider_and_combined(tmp_path: Path):
    tm = _seed(tmp_path, ROWS)
    page = tm.page(provider="chatgpt")
    assert [t["thread_id"] for t in page["threads"]] == ["t-new", "t-mid"]
    assert page["total"] == 2
    both = tm.page(provider="chatgpt", q="天气")
    assert [t["thread_id"] for t in both["threads"]] == ["t-mid"]
    assert both["total"] == 1


def test_page_keeps_active_max_fields(tmp_path: Path):
    tm = _seed(tmp_path, ROWS)
    page = tm.page()
    assert page["active"] == 0 and page["max"] == tm.max_threads


# ---------- 路由层：/admin/threads 查询参数 ----------


def _app(tmp_path: Path):
    import yaml
    from fastapi.testclient import TestClient

    from ai_web2api.main import create_app

    root = Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((root / "config.fake.yaml").read_text())
    cfg["profiles_dir"] = str(tmp_path)
    cfg["providers"]["fake"]["url"] = f"file://{root / 'tests' / 'fake_chat.html'}"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return TestClient(create_app(str(cfg_path)))


def test_api_threads_supports_query_params(tmp_path: Path):
    store = ThreadStore(tmp_path / "threads.db")
    for tid, provider, title, ts in ROWS:
        store.upsert_thread(tid, provider, model=f"{provider}-web", title=title)
        store._conn.execute("UPDATE threads SET updated_at = ? WHERE thread_id = ?", (ts, tid))
    store._conn.commit()
    store.close()

    c = _app(tmp_path)
    page = c.get("/admin/threads", params={"limit": 2}).json()
    assert page["total"] == 3 and page["limit"] == 2 and page["has_more"] is True
    assert [t["thread_id"] for t in page["threads"]] == ["t-new", "t-mid"]
    assert "active" in page and "max" in page  # 旧字段保留

    assert c.get("/admin/threads", params={"q": "天气"}).json()["total"] == 1
    assert c.get("/admin/threads", params={"provider": "deepseek"}).json()["total"] == 1
    # 旧的"不带参数"仍返回全部（向后兼容）
    assert len(c.get("/admin/threads").json()["threads"]) == 3
