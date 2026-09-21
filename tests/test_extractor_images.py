"""图片提取：ChatGPT 生图响应要提取成干净的 `![](原图URL)`。

运行：.venv/bin/python -m pytest tests/test_extractor_images.py -v
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "ai_web2api" / "browser" / "extractor.py"


def test_image_extraction_is_clean():
    js = SRC.read_text(encoding="utf-8")
    # alt 可能是原图 URL、src 是缩略图 → 用原图并把 URL alt 清掉
    assert "seenImgs" in js, "图片未去重"
    assert "purpose=inline" in js, "未优先使用 alt 里的原图（fullsize）"
    # 图片常被 UI 按钮包裹（"Add to Favorites"）→ 只留图片、丢按钮文案
    assert 'tag === "button"' in js, "未过滤按钮 UI 文案"
    # 空 alt 的规范写法 `![](url)`（供 markdown 渲染器识别为图片）
    assert '"![" + alt + "](" + src + ")"' in js
