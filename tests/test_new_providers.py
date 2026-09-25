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


def test_gemini_calibrated_and_enabled():
    """Gemini 游客态即可用 → 启用；不能配 logged_out（否则会把可用状态拒掉）。"""
    from ai_web2api.providers.gemini import GeminiProvider
    from ai_web2api.providers.registry import DRIVERS

    assert DRIVERS["gemini"] is GeminiProvider
    assert GeminiProvider.session_url_pattern == r"/app/([0-9a-fA-F]{8,})"
    p = _cfg("gemini")
    assert p.enabled is True
    assert any("ql-editor" in x for x in p.selectors.input), "实测 Quill 输入框别删"
    assert any("model-response-text" in x for x in p.selectors.response_container)
    assert p.selectors.logged_out == [], "游客态是合法可用状态，不能当未登录"
    assert p.selectors.type_prompt is True


def test_gemini_login_detect_ignores_guest_placeholder_avatar():
    """登录自动检测不得用裸头像选择器：游客态占位头像是 `img.user-icon` 的
    default-user，未登录也命中 → 会「还没登录就保存/导入 state」（实测 bug）。

    必须用「无登录入口」条件约束头像，或只认 SignOutOptions。
    """
    p = _cfg("gemini")
    detect = p.login.detect
    assert detect, "Gemini 应保留自动检测（真登录标记）"
    # 裸 `img[alt*="个人资料"]` / `img.user-icon` 会命中最游客占位头像，禁止单独出现
    assert 'img[alt*="个人资料"]' not in detect
    assert "img.user-icon" not in detect, "不能单独用头像判定（游客占位头像会命中）"
    # 若用头像，必须叠加「页面上没有登录链接」的条件
    avatar_sels = [s for s in detect if "img.user-icon" in s]
    assert avatar_sels, "应有一个带约束的头像选择器"
    assert all("ServiceLogin" in s for s in avatar_sels), (
        "头像选择器必须带「页面上没有登录链接」的条件"
    )


def test_claude_calibrated_scaffold():
    """Claude：手动登录 + ProseMirror 输入 + 助手侧正文容器。"""
    from ai_web2api.providers.claude import ClaudeProvider
    from ai_web2api.providers.registry import DRIVERS

    assert DRIVERS["claude"] is ClaudeProvider
    assert ClaudeProvider.session_url_pattern == r"/chat/([0-9a-fA-F-]{8,})"
    p = _cfg("claude")
    assert p.url.startswith("https://claude.ai")
    assert p.login.mode == "manual"
    assert p.selectors.type_prompt is True, "contenteditable 需逐字输入"
    assert any("ProseMirror" in s for s in p.selectors.input)
    assert any("font-claude-response" in s for s in p.selectors.response_container)
    assert p.selectors.stream_content is False, "markdown 重渲染多 → 缓冲后发"
    assert p.network.url_pattern, "Claude 走网络 SSE 抓取"
    assert any("action-bar-copy" in s for s in p.selectors.done_toolbar)


def test_claude_sse_parse():
    """Claude content_block_delta → (thinking, content)。"""
    from ai_web2api.providers.claude import ClaudeProvider

    sse = (
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"thinking_delta","thinking":"想"}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,'
        '"delta":{"type":"text_delta","text":"收"}}\n\n'
        'event: content_block_delta\ndata: {"type":"content_block_delta","index":1,'
        '"delta":{"type":"text_delta","text":"到"}}\n\n'
        'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    thinking, content = ClaudeProvider._parse_sse_snapshot(sse)
    assert thinking == "想"
    assert content == "收到"


def test_domestic_providers_bypass_proxy():
    """国内站点直连（不走 browser.proxy）；Claude 等仍走代理。"""
    for name in ("deepseek", "doubao", "kimi", "glm"):
        assert _cfg(name).proxy is False, f"{name} 应直连"
    assert _cfg("claude").proxy is True


def test_proxy_applied_per_provider():
    """浏览器 context 级代理：Claude 走 proxy，国内 provider set_direct 后为空。"""
    from pathlib import Path

    from ai_web2api.browser.manager import BrowserManager
    from ai_web2api.config import BrowserConfig

    bm = BrowserManager(BrowserConfig(proxy="http://p:7890"), Path("/tmp/x"))
    assert bm._proxy_kwargs("claude") == {"proxy": {"server": "http://p:7890"}}
    bm.set_direct("deepseek")
    assert bm._proxy_kwargs("deepseek") == {}


def test_docs_and_readme_reference_both():
    for name in ("doubao", "glm", "gemini", "claude"):
        assert (ROOT / "docs" / f"PROVIDER_{name.upper()}.md").is_file()
    for f in ("README.md", "README.zh-CN.md"):
        text = (ROOT / f).read_text(encoding="utf-8")
        for doc in (
            "docs/PROVIDER_DOUBAO.md",
            "docs/PROVIDER_GLM.md",
            "docs/PROVIDER_GEMINI.md",
            "docs/PROVIDER_CLAUDE.md",
        ):
            assert doc in text, f"{f} 缺少 {doc}"
