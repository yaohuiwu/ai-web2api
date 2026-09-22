"""仓库信息（后端缓存拉取）：解析 / 缓存 / 失败降级 / 路由。

运行：.venv/bin/python -m pytest tests/test_repo_info.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ai_web2api.core.repo_info import clear_cache, parse_github_repo, repo_info

REPO = "https://github.com/yaohuiwu/ai-web2api"


def setup_function(_):
    clear_cache()


def test_parse_github_repo():
    assert parse_github_repo(REPO) == ("yaohuiwu", "ai-web2api")
    assert parse_github_repo(REPO + ".git") == ("yaohuiwu", "ai-web2api")
    assert parse_github_repo("git@github.com:owner/repo.git") == ("owner", "repo")
    assert parse_github_repo("https://example.com/x/y") is None
    assert parse_github_repo(None) is None


def test_repo_info_without_url_is_all_none():
    info = repo_info(None)
    assert info == {"url": None, "stars": None, "forks": None, "open_issues": None, "fetched_at": None}


def test_repo_info_caches_success():
    calls = []

    def fake(url):
        calls.append(url)
        return {"stars": 42, "forks": 3, "open_issues": 1, "full_name": "yaohuiwu/ai-web2api"}

    a = repo_info(REPO, now=1000.0, fetcher=fake)
    b = repo_info(REPO, now=1010.0, fetcher=fake)          # 命中缓存
    assert a["stars"] == 42 and b["stars"] == 42
    assert len(calls) == 1
    c = repo_info(REPO, now=1000.0 + 601, fetcher=fake)    # 超过 TTL → 重新拉
    assert len(calls) == 2 and c["stars"] == 42


def test_repo_info_failure_degrades_and_caches_briefly():
    def boom(url):
        raise RuntimeError("rate limited")

    a = repo_info(REPO, now=1000.0, fetcher=boom)
    assert a["stars"] is None and "rate limited" in a["error"] and a["url"] == REPO
    b = repo_info(REPO, now=1030.0, fetcher=boom)          # 失败结果也短暂缓存（60s）
    assert b is a


# ---------- 路由 ----------


def _app(tmp_path: Path, repo_url: str | None = REPO):
    from ai_web2api.main import create_app

    root = Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((root / "config.fake.yaml").read_text())
    cfg["profiles_dir"] = str(tmp_path)
    cfg["providers"]["fake"]["url"] = f"file://{root / 'tests' / 'fake_chat.html'}"
    if repo_url is not None:
        cfg.setdefault("server", {})["repo_url"] = repo_url
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return TestClient(create_app(str(cfg_path)))


def test_admin_repo_endpoint(tmp_path: Path, monkeypatch):
    import ai_web2api.core.repo_info as ri

    monkeypatch.setattr(
        ri, "fetch_repo_stats",
        lambda url, timeout=5.0: {"stars": 7, "forks": 1, "open_issues": 0, "full_name": "yaohuiwu/ai-web2api"},
    )
    body = _app(tmp_path).get("/admin/repo").json()
    assert body["url"] == REPO and body["stars"] == 7


def test_admin_repo_endpoint_unconfigured(tmp_path: Path):
    body = _app(tmp_path, repo_url=None).get("/admin/repo").json()
    assert body["url"] is None and body["stars"] is None
