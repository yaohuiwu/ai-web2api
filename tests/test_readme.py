"""README：中英文双语 + 顶部界面截图（默认英文）。

运行：.venv/bin/python -m pytest tests/test_readme.py -v
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMG = "docs/assets/ui.png"


def test_bilingual_readme_defaults_to_english():
    en = (ROOT / "README.md").read_text(encoding="utf-8")
    zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    # 默认英文：README.md 顶部有 English 标识 + 指向中文版；反之亦然
    assert "**English**" in en.split("\n")[2], en.split("\n")[2]
    assert "[中文](README.zh-CN.md)" in en
    assert "**中文**" in zh.split("\n")[2], zh.split("\n")[2]
    assert "[English](README.md)" in zh
    # pyproject 的 readme 指向英文版（PyPI 长描述）
    assert 'readme = "README.md"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def test_readme_shows_ui_screenshot_at_top():
    assert (ROOT / IMG).is_file(), f"缺少截图 {IMG}"
    for name in ("README.md", "README.zh-CN.md"):
        lines = (ROOT / name).read_text(encoding="utf-8").split("\n")
        img_line = next((i for i, ln in enumerate(lines) if IMG in ln), None)
        assert img_line is not None, f"{name} 未引用界面截图"
        assert img_line <= 12, f"{name} 截图应放在最上方（当前第 {img_line + 1} 行）"
        body = "\n".join(lines)
        assert body.count("```") % 2 == 0, f"{name} 代码块未闭合（会破坏 GitHub 渲染）"


def test_readme_documents_login_shortcuts():
    """中英文都给出「一条命令登录」与短命令（避免只有中文版可查）。"""
    for name in ("README.md", "README.zh-CN.md"):
        body = (ROOT / name).read_text(encoding="utf-8")
        assert "./scripts/login.sh" in body and "ai-web2api login" in body, name
