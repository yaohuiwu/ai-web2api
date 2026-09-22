"""新增 provider 脚手架（doubao / glm）：注册 / 配置 / 文档。

运行：.venv/bin/python -m pytest tests/test_new_providers.py -v
"""

from __future__ import annotations

import pathlib
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
def test_manual_login(name: str):
    """两者都无密码登录（GLM 另有 WAF；豆包有区域限制）。"""
    p = _cfg(name)
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
    assert p.selectors.login_check == [], "留空（用 input 判定）"
    assert p.selectors.logged_out, "游客态也有输入框 → 必须配反向标记，否则登录态误判为真"


def test_glm_disabled_by_default_with_reason():
    """GLM 默认禁用：WAF 拦自动化 + 票据仅 ~30 分钟（详见 docs/PROVIDER_GLM.md）。"""
    cfg = (pathlib.Path(__file__).resolve().parent.parent / "config.yaml").read_text(encoding="utf-8")
    idx = cfg.index("  glm:")
    block = cfg[max(0, idx - 900): idx + 400]      # 禁用原因写在 glm: 之前的注释里
    assert _cfg("glm").enabled is False, "GLM 默认禁用"
    assert "30 分钟" in block and "WAF" in block, "配置里必须写明禁用原因（WAF + 票据 30 分钟）"


def test_glm_calibrated_selectors():
    """GLM 已校准（需宿主人工过 WAF 一次 + 导入 state）：选择器别删。"""
    p = _cfg("glm")
    assert p.url.startswith("https://chatglm.cn")
    assert any("textarea" in s for s in p.selectors.input), "实测是可见 textarea"
    assert any("thinking-content" in s for s in p.selectors.response_container), \
        "正文选择器必须排除思考（思考也在 .markdown-body 里）"
    assert p.selectors.login_check, "登录后侧栏有用户名 → 必须配 login_check"
    assert p.selectors.send_button == [], "实测无发送按钮：用 Enter"


def test_glm_has_no_session_pattern_and_waf_documented():
    """GLM 被 WAF 滑块拦 → 不启用 thread 恢复；文档必须写清原因与变通。"""
    from ai_web2api.providers.glm import GlmProvider

    assert GlmProvider.session_url_pattern is None
    doc = (ROOT / "docs" / "PROVIDER_GLM.md").read_text(encoding="utf-8")
    assert "WAF" in doc and "滑动" in doc and "人工" in doc
    assert "headful" in doc, "要写明 headful 也被拦（不是 headless 特征问题）"
    assert "复用" in doc, "要写明 WAF cookie 导入后可复用（实测）"


def test_gemini_enabled_guest_mode():
    """Gemini 游客态即可用 → 默认启用；不能配 logged_out（否则会把可用状态拒掉）。"""
    from ai_web2api.providers.gemini import GeminiProvider
    from ai_web2api.providers.registry import DRIVERS

    assert DRIVERS["gemini"] is GeminiProvider
    assert GeminiProvider.session_url_pattern == r"/app/([0-9a-fA-F]{8,})"
    p = _cfg("gemini")
    assert p.enabled is True, "游客态可用 → 默认启用"
    assert any("ql-editor" in x for x in p.selectors.input), "实测 Quill 输入框别删"
    assert any("model-response-text" in x for x in p.selectors.response_container)
    assert p.selectors.logged_out == [], "游客态是合法可用状态，不能当未登录"
    assert p.selectors.type_prompt is True


def test_docs_and_readme_reference_both():
    for name in ("doubao", "glm", "gemini"):
        assert (ROOT / "docs" / f"PROVIDER_{name.upper()}.md").is_file()
    for f in ("README.md", "README.zh-CN.md"):
        text = (ROOT / f).read_text(encoding="utf-8")
        for doc in ("docs/PROVIDER_DOUBAO.md", "docs/PROVIDER_GLM.md", "docs/PROVIDER_GEMINI.md"):
            assert doc in text, f"{f} 缺少 {doc}"
