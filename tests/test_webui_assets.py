"""webui 结构约束：HTML / CSS / JS 分离，且引用的资源都存在。

运行：.venv/bin/python -m pytest tests/test_webui_assets.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

WEBUI = Path(__file__).resolve().parent.parent / "src" / "ai_web2api" / "webui"
HTML_FILES = ("index.html", "playground.html")


def test_no_inline_style_or_script():
    """HTML 只负责结构：不允许内联 <style> 或带内容的 <script>。"""
    for name in HTML_FILES:
        s = (WEBUI / name).read_text(encoding="utf-8")
        assert "<style>" not in s, f"{name} 仍有内联 <style>"
        # 带内容的内联 <script>（有 src 的 <script src=...></script> 允许）
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", s), f"{name} 仍有内联 <script>"


def test_referenced_assets_exist():
    for name in HTML_FILES:
        s = (WEBUI / name).read_text(encoding="utf-8")
        refs = re.findall(r'(?:href|src)="(assets/[^"]+)"', s)
        assert refs, f"{name} 未引用任何 assets 资源"
        for ref in refs:
            assert (WEBUI / ref).is_file(), f"{name} 引用的 {ref} 不存在"


def test_shared_assets_present():
    for rel in ("assets/css/base.css", "assets/js/common.js"):
        assert (WEBUI / rel).is_file(), f"缺少共享资源 {rel}"
