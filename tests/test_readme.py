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


def test_readme_features_block_and_provider_table():
    """开头要有 Feature 块 + provider 支持状态表（Qwen 如实标注不稳定/慢/登录易被墙）。"""
    cases = {
        "README.md": {
            "feature_head": "## Features",
            "table_head": "## Supported providers",
            "quick_start": "## Quick start",
            "tokens": ["DeepSeek", "ChatGPT", "Qwen", "unstable", "slow", "blocked", "enabled: false"],
        },
        "README.zh-CN.md": {
            "feature_head": "## 功能特性",
            "table_head": "## 支持的 provider",
            "quick_start": "## 快速开始",
            "tokens": ["DeepSeek", "ChatGPT", "Qwen", "不稳定", "登录容易被墙", "enabled: false"],
        },
    }
    for name, spec in cases.items():
        body = (ROOT / name).read_text(encoding="utf-8")
        for t in spec["tokens"]:
            assert t in body, f"{name} 缺少 {t!r}"
        # 三块顺序：Features → providers 表 → Quick start（都在开头）
        i_f = body.index(spec["feature_head"])
        i_t = body.index(spec["table_head"])
        i_q = body.index(spec["quick_start"])
        assert i_f < i_t < i_q, f"{name} 段落顺序不对（Features={i_f} 表={i_t} QuickStart={i_q}）"
        assert i_f < 2500, f"{name} 的 Feature 块应位于开头（当前字符偏移 {i_f}）"
