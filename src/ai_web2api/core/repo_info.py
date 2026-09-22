"""GitHub 仓库信息（UI 顶部 Star 入口 + 星标数）。

放在后端做：**一次拉取 + 缓存**，局域网/手机访问 UI 也能看到；失败不影响界面（返回 error）。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request

logger = logging.getLogger(__name__)

CACHE_TTL = 600.0        # 成功结果缓存 10 分钟（GitHub 匿名限流 60/h）
ERROR_TTL = 60.0         # 失败也短暂缓存，避免每次刷新都打 GitHub
_GITHUB_RE = re.compile(r"github\.com[/:]([^/\s]+)/([^/\s#?]+)")
_cache: dict[str, tuple[float, dict]] = {}


def parse_github_repo(url: str | None) -> tuple[str, str] | None:
    """从仓库 URL 解析 ``(owner, repo)``；非 GitHub 地址返回 None。"""
    if not url:
        return None
    m = _GITHUB_RE.search(url.strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    return owner, repo[:-4] if repo.endswith(".git") else repo


def _fetch_json(api_url: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-web2api",  # GitHub 要求带 UA
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_repo_stats(url: str, *, timeout: float = 5.0) -> dict:
    """真实拉取（测试可替换）：返回 ``{stars, forks, open_issues}``。"""
    parsed = parse_github_repo(url)
    if not parsed:
        raise ValueError(f"不是 GitHub 仓库地址：{url!r}")
    owner, repo = parsed
    data = _fetch_json(f"https://api.github.com/repos/{owner}/{repo}", timeout=timeout)
    return {
        "full_name": data.get("full_name") or f"{owner}/{repo}",
        "stars": data.get("stargazers_count"),
        "forks": data.get("forks_count"),
        "open_issues": data.get("open_issues_count"),
    }


def repo_info(
    url: str | None,
    *,
    ttl: float = CACHE_TTL,
    error_ttl: float = ERROR_TTL,
    now: float | None = None,
    fetcher=None,
) -> dict:
    """带缓存的仓库信息；``url`` 为空 → 全部为 None（UI 隐藏）。"""
    if not url:
        return {"url": None, "stars": None, "forks": None, "open_issues": None, "fetched_at": None}
    now = time.time() if now is None else now
    cached = _cache.get(url)
    if cached and now - cached[0] < (ttl if cached[1].get("stars") is not None else error_ttl):
        return cached[1]

    fetch = fetcher or fetch_repo_stats
    try:
        stats = fetch(url)
        info = {"url": url, "fetched_at": now, **stats}
    except Exception as exc:  # noqa: BLE001  网络/限流失败不能让 UI 崩
        logger.info("拉取仓库信息失败（%s）：%s", url, exc)
        info = {
            "url": url,
            "stars": None,
            "forks": None,
            "open_issues": None,
            "fetched_at": now,
            "error": str(exc)[:200],
        }
    if len(_cache) > 16:
        _cache.clear()
    _cache[url] = (now, info)
    return info


def clear_cache() -> None:
    """测试用：清空缓存。"""
    _cache.clear()
