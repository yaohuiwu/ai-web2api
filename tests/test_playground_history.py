"""Playground 会话列表面板：改为手动刷新（不再轮询）+ 切换会话回填历史。

静态约束（无需浏览器）：避免以后改动时又加回定时轮询 / 丢掉历史渲染。
运行：.venv/bin/python -m pytest tests/test_playground_history.py -v
"""

from __future__ import annotations

from pathlib import Path

WEBUI = Path(__file__).resolve().parent.parent / "src" / "ai_web2api" / "webui"
PLAYGROUND_HTML = WEBUI / "playground.html"
PLAYGROUND_JS = WEBUI / "assets" / "js" / "playground.js"


def test_has_refresh_button_and_no_polling():
    html = PLAYGROUND_HTML.read_text(encoding="utf-8")
    js = PLAYGROUND_JS.read_text(encoding="utf-8")
    assert 'id="refreshThreads"' in html, "缺少刷新按钮"
    assert '$("#refreshThreads").onclick = refreshThreads' in js, "刷新按钮未绑定"
    assert "setInterval(refreshThreads" not in js, "不应再定时轮询会话列表"


def test_switch_thread_renders_history_from_api():
    js = PLAYGROUND_JS.read_text(encoding="utf-8")
    assert "function renderHistory" in js, "缺少历史渲染函数"
    assert "/messages" in js, "未请求 /admin/threads/{id}/messages"
