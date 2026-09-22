"""新增 provider 脚手架（doubao / glm）：注册 / 配置 / 文档。

运行：.venv/bin/python -m pytest tests/test_new_providers.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _cfg(name: str):
    from ai_web2api.config import load_config

    return next(p for p in load_config(ROOT / "config.yaml").providers if p.name == name)


def test_drivers_registered():
    from ai_web2api.providers.doubao import DoubaoProvider
    from ai_web2api.providers.glm import GlmProvider
    from ai_web2api.providers.registry import DRIVERS
    from ai_web2api.providers.webchat import WebChatProvider

    assert DRIVERS["doubao"] is DoubaoProvider
    assert DRIVERS["glm"] is GlmProvider
    assert issubclass(DoubaoProvider, WebChatProvider)
    assert issubclass(GlmProvider, WebChatProvider)


@pytest.mark.parametrize("name", ["doubao", "glm"])
def test_disabled_and_manual_login(name: str):
    """两者都无密码登录；选择器未校准前必须默认禁用。"""
    p = _cfg(name)
    assert p.enabled is False, f"{name} 待校准，必须默认禁用"
    assert p.login.mode == "manual"
    assert "login" in p.login.hint or "登录" in p.login.hint


def test_doubao_measured_selectors_and_url_template():
    from ai_web2api.providers.doubao import DoubaoProvider

    p = _cfg("doubao")
    assert p.url.startswith("https://www.doubao.com")
    assert any("contenteditable" in s for s in p.selectors.input), "实测的 tiptap/ProseMirror 输入框别删"
    assert p.selectors.type_prompt is True, "contenteditable 需逐字输入"
    assert DoubaoProvider.session_url_pattern == r"/chat/(\d{6,})"
    # 区域限制：未登录时容器内没有输入框 → 不能把"游客态"当已登录
    assert p.selectors.login_check == [], "未校准前留空（用 input 判定）"


def test_glm_has_no_session_pattern_and_waf_documented():
    """GLM 被 WAF 滑块拦 → 不启用 thread 恢复；文档必须写清原因与变通。"""
    from ai_web2api.providers.glm import GlmProvider

    assert GlmProvider.session_url_pattern is None
    doc = (ROOT / "docs" / "PROVIDER_GLM.md").read_text(encoding="utf-8")
    assert "WAF" in doc and "滑动" in doc and "人工" in doc
    assert "headful" in doc, "要写明 headful 也被拦（不是 headless 特征问题）"


def test_docs_and_readme_reference_both():
    for name, provider in (("doubao", "Doubao"), ("glm", "GLM")):
        assert (ROOT / "docs" / f"PROVIDER_{name.upper()}.md").is_file()
    for f in ("README.md", "README.zh-CN.md"):
        text = (ROOT / f).read_text(encoding="utf-8")
        assert "docs/PROVIDER_DOUBAO.md" in text and "docs/PROVIDER_GLM.md" in text, f
