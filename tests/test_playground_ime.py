"""Playground 输入框 IME（输入法）组词保护。

回归：中文/日文输入法组词时按回车是「选词上屏」，不应触发发送。
运行：.venv/bin/python -m pytest tests/test_playground_ime.py -v
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

HTML = Path(__file__).resolve().parent.parent / "src" / "ai_web2api" / "webui" / "playground.html"


def test_input_guards_ime_composition():
    s = HTML.read_text(encoding="utf-8")
    assert "compositionstart" in s, "缺少 compositionstart 监听"
    assert "compositionend" in s, "缺少 compositionend 监听"
    assert "e.isComposing" in s, "缺少 isComposing 判断"
    assert "e.keyCode === 229" in s, "缺少 keyCode 229（组词中）判断"


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 校验前端 JS 语法")
def test_playground_js_parses():
    """整段 <script> 能被 node 解析（防手改引入语法错误）。"""
    s = HTML.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*)</script>", s, re.S)
    assert m is not None, "未找到 <script> 块"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(m.group(1))
        path = f.name
    r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
