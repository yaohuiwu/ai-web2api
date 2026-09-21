"""Playground 输入框 IME（输入法）组词保护。

回归：中文/日文输入法组词时按回车是「选词上屏」，不应触发发送。
样式/脚本已分离到 assets/，此处校验 JS。
运行：.venv/bin/python -m pytest tests/test_playground_ime.py -v
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEBUI = Path(__file__).resolve().parent.parent / "src" / "ai_web2api" / "webui"
JS_DIR = WEBUI / "assets" / "js"
PLAYGROUND_JS = JS_DIR / "playground.js"


def test_input_guards_ime_composition():
    s = PLAYGROUND_JS.read_text(encoding="utf-8")
    assert "compositionstart" in s, "缺少 compositionstart 监听"
    assert "compositionend" in s, "缺少 compositionend 监听"
    assert "e.isComposing" in s, "缺少 isComposing 判断"
    assert "e.keyCode === 229" in s, "缺少 keyCode 229（组词中）判断"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 校验前端 JS 语法")
def test_all_webui_js_parses():
    """所有前端 JS 都能被 node 解析（防手改引入语法错误）。"""
    files = sorted(JS_DIR.glob("*.js"))
    assert files, "assets/js 下没有 JS 文件"
    for f in files:
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{f.name}: {r.stderr}"
