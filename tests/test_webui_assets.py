"""webui 结构约束：HTML / CSS / JS 分离，且引用的资源都存在。

运行：.venv/bin/python -m pytest tests/test_webui_assets.py -v
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

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


def test_markdown_renders_images_and_autolinks():
    js = (WEBUI / "assets/js/markdown.js").read_text(encoding="utf-8")
    assert 'class="md-img"' in js, "markdown 未渲染图片（![](url) → <img>）"
    assert "referrerpolicy" in js, "图片未带 referrerpolicy（外站图可能 403）"
    assert "裸 URL 自动链接" in js, "未把裸 URL 自动变成链接（DeepSeek 思考引用）"


_JS_PROBE = r"""
global.esc = (s) => String(s ?? "");
eval(require("fs").readFileSync(process.argv[2], "utf8"));
const U = "https://images.example.com/a_b_c/d_e/f?purpose=fullsize";
const out = md("![](" + U + ")");
if (!out.includes('src="' + U + '"')) throw new Error("image URL corrupted: " + out);
if (out.includes("<em>")) throw new Error("emphasis leaked into URL: " + out);
if (!out.includes('target="_blank"')) throw new Error("attr corrupted: " + out);
const link = md("见 https://example.com/a_b_c 与 `https://x_y_z`");
if (!link.includes('href="https://example.com/a_b_c"')) throw new Error("autolink broken: " + link);
if (!link.includes("<code>https://x_y_z</code>")) throw new Error("code should not link: " + link);
console.log("ok");
"""


def test_markdown_does_not_corrupt_urls(tmp_path):
    """回归：URL 里的下划线不能被斜体规则误伤（图片 src 被拆坏会显示不出来）。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用")
    script = tmp_path / "probe.js"
    script.write_text(_JS_PROBE, encoding="utf-8")
    subprocess.run(
        [node, str(script), str(WEBUI / "assets/js/markdown.js")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_playground_shows_waiting_indicator():
    """等待响应时要有 UI 提示（动点 + 秒数），避免看起来卡死。"""
    js = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")
    css = (WEBUI / "assets/css/playground.css").read_text(encoding="utf-8")
    assert "function showPending" in js
    assert "等待响应…" in js
    assert "stopPending" in js
    # 非流式也要显示（整包等待最容易以为卡死）
    assert "sendNonStream" in js and "showPending(pendingDiv)" in js
    assert ".pending" in css and "pending-bounce" in css
