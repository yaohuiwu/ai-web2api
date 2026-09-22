"""thread 恢复（resume）：等输入框就绪，而不是死等固定时长。

回归背景：ChatGPT 的 SPA 恢复页渲染 >2s，原先固定 `wait_for_timeout(2000)` + 立即匹配会误判
"恢复失败" → 退回新会话（用户表现为"没有接着原来的会话聊"）。
运行：.venv/bin/python -m pytest tests/test_thread_resume.py -v
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_web2api.config import ServerConfig
from ai_web2api.core.errors import ThreadMismatchError
from ai_web2api.core.store import ThreadStore
from ai_web2api.core.threads import ThreadManager


class _Page:
    def __init__(self) -> None:
        self.url = "https://example.com/"
        self.goto_calls: list[str] = []
        self.reloads = 0
        self.closed = False

    async def goto(self, url: str, **_kw) -> None:
        self.goto_calls.append(url)
        self.url = url

    async def reload(self, **_kw) -> None:
        self.reloads += 1

    async def close(self) -> None:
        self.closed = True

    def is_closed(self) -> bool:
        return self.closed


class _Provider:
    """恢复路径需要的最小 provider 接口。"""

    name = "chatgpt"
    session_url_pattern = r"/c/([0-9a-zA-Z-]{8,})"
    locale = "en-US"

    def __init__(self) -> None:
        self.cfg = SimpleNamespace(selectors=SimpleNamespace(input=[".composer"]))
        self.opened: list[_Page] = []
        self.new_pages = 0

    # 让 _restore_or_new_page 里的 provider.browser.open_page 可用
    @property
    def browser(self):
        return self

    async def open_page(self, _name: str, **_kw) -> _Page:
        page = _Page()
        self.opened.append(page)
        return page

    async def open_chat_page(self) -> _Page:
        self.new_pages += 1
        return _Page()

    def session_url(self, url_id: str) -> str:
        return f"https://chatgpt.com/c/{url_id}"

    def init_scripts(self):
        return []


def _manager(tmp_path: Path, *, url_id="68fe1234-aaaa-bbbb") -> ThreadManager:
    tm = ThreadManager(ServerConfig(thread_persist=True), tmp_path)
    store = ThreadStore(tmp_path / "threads.db")
    store.upsert_thread("t1", "chatgpt", model="gpt-5-web", title="hi", url_id=url_id)
    store.close()
    return tm


@pytest.mark.asyncio
async def test_restore_waits_for_input(tmp_path: Path, monkeypatch):
    """输入框稍晚出现（ChatGPT 实测 >2s）→ 仍应判定恢复成功。"""
    from ai_web2api.browser import extractor

    provider = _Provider()
    calls: list[tuple] = []

    async def fake_wait(page, selectors, timeout=15.0, poll=0.3):
        calls.append(("wait", selectors, timeout))
        return ".composer"          # 等待后成功

    monkeypatch.setattr(extractor, "wait_first_match", fake_wait)
    page, restored = await _manager(tmp_path)._restore_or_new_page("t1", provider, "gpt-5-web")

    assert restored is True and provider.new_pages == 0
    assert page.goto_calls == ["https://chatgpt.com/c/68fe1234-aaaa-bbbb"]
    assert calls and calls[0][2] >= 10, "必须给 SPA 足够的渲染时间（不能再是固定 2s）"


@pytest.mark.asyncio
async def test_restore_reloads_once_then_falls_back(tmp_path: Path, monkeypatch):
    """第一次等不到输入框 → 重载再等；仍失败才退回新会话。"""
    from ai_web2api.browser import extractor

    provider = _Provider()
    results = [None, None]

    async def fake_wait(page, selectors, timeout=15.0, poll=0.3):
        return results.pop(0) if results else None

    monkeypatch.setattr(extractor, "wait_first_match", fake_wait)
    page, restored = await _manager(tmp_path)._restore_or_new_page("t1", provider, "gpt-5-web")

    assert restored is False and provider.new_pages == 1
    assert provider.opened[0].reloads == 1, "应重载一次再试"
    assert provider.opened[0].closed is True, "失败页面要关掉，避免泄漏"


@pytest.mark.asyncio
async def test_restore_second_attempt_succeeds(tmp_path: Path, monkeypatch):
    """重载后成功 → 仍算恢复（覆盖"首帧不完整"的站点）。"""
    from ai_web2api.browser import extractor

    provider = _Provider()
    results = [None, ".composer"]

    async def fake_wait(page, selectors, timeout=15.0, poll=0.3):
        return results.pop(0)

    monkeypatch.setattr(extractor, "wait_first_match", fake_wait)
    page, restored = await _manager(tmp_path)._restore_or_new_page("t1", provider, "gpt-5-web")

    assert restored is True and provider.new_pages == 0
    assert provider.opened[0].reloads == 1 and provider.opened[0].closed is False


@pytest.mark.asyncio
async def test_model_mismatch_on_restore_is_409(tmp_path: Path):
    provider = _Provider()
    with pytest.raises(ThreadMismatchError):
        await _manager(tmp_path)._restore_or_new_page("t1", provider, "o3-web")
