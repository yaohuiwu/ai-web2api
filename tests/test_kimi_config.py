"""Kimi provider：注册 / 配置 / 实测选择器（改版别把校准结果删掉）。

运行：.venv/bin/python -m pytest tests/test_kimi_config.py -v
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _kimi():
    from ai_web2api.config import load_config

    return next(p for p in load_config(ROOT / "config.yaml").providers if p.name == "kimi")


def test_driver_registered_and_enabled():
    from ai_web2api.providers.kimi import KimiProvider
    from ai_web2api.providers.registry import DRIVERS
    from ai_web2api.providers.webchat import WebChatProvider

    assert DRIVERS["kimi"] is KimiProvider
    assert issubclass(KimiProvider, WebChatProvider)
    assert KimiProvider.session_url_pattern == r"/chat/([0-9a-fA-F-]{8,})"
    assert _kimi().enabled is False, "站点高峰排队/限流严重 → 默认禁用，需要时手动开"


def test_login_is_manual_with_hint():
    k = _kimi()
    assert k.url.startswith("https://www.kimi.com")
    assert k.login.mode == "manual", "Kimi 只有扫码/短信登录（易盾验证码），无法自动化"
    assert "login" in k.login.hint


def test_calibrated_selectors():
    """以下都是实测校准结果，改版前不要删。"""
    k = _kimi()
    s = k.selectors
    assert ".chat-input-editor" in s.input
    assert s.type_prompt is True, "contenteditable 需逐字输入"
    assert ".send-button-container" in s.send_button
    # 正文：排除带 toolcall-content-text 的（那是思考），否则思考会混进正文
    assert any(":not(.toolcall-content-text)" in x for x in s.response_container), s.response_container
    assert all(".segment-content" not in x for x in s.response_container), "segment-content 会混入思考"
    assert s.stream_content is False, "思考与正文同段 → 必须缓冲后一次发"
    assert ".user-area__main img" in s.login_check, "未登录也能输入，登录判定必须靠头像"
    assert s.stop_button == [], "站点无停止按钮 → 靠稳定性判定"
    assert "input.hidden-input[type='file']" in s.upload_input
    assert k.session_url == "{base}/chat/{id}", "会话 URL 模板（thread 恢复）"


def test_doc_and_readme_mention_kimi():
    doc = ROOT / "docs" / "PROVIDER_KIMI.md"
    assert doc.is_file()
    body = doc.read_text(encoding="utf-8")
    assert "toolcall-content-text" in body, "文档要写明正文/思考的区分点"
    assert "重新校准" in body, "文档要保留改版后的校准路径"
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "Kimi" in text and "docs/PROVIDER_KIMI.md" in text, name


def test_enabling_kimi_registers_provider_and_exposes_model():
    """注册表能构造 kimi 并暴露模型（不需要真浏览器）。"""
    from ai_web2api.config import AppConfig
    from ai_web2api.providers.registry import ProviderRegistry

    class _StubBrowser:
        default_locale = "zh-CN"

    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["providers"] = {"kimi": copy.deepcopy(raw["providers"]["kimi"])}
    raw["providers"]["kimi"]["enabled"] = True      # 默认禁用，这里显式打开以验证注册
    cfg = AppConfig.model_validate(raw)
    reg = ProviderRegistry(cfg, _StubBrowser())  # type: ignore[arg-type]
    assert "kimi" in reg.providers()
    assert "kimi-web" in reg.get_provider("kimi").exposed_models


def test_thinking_selector_is_narrow_first():
    """思考选择器必须**先窄后宽**：`.toolcall-rollup` 会把「正在思考中 / 使用 N 个工具」等
    UI 文案混进 reasoning_content（实测把 '正在思考中' 粘在推理前面）。
    """
    sels = _kimi().selectors.thinking_container
    assert sels, "Kimi 应有 thinking 选择器"
    assert "toolcall-content-text" in sels[0], f"首选必须是纯推理容器，当前：{sels[0]}"
    assert sels[0] != ".chat-content-item-assistant .toolcall-rollup"
    # 宽的只作兜底（排后面）
    assert all("toolcall-rollup" not in s for s in sels[:1])
