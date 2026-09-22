"""Kimi provider 脚手架：注册 / 配置 / 实测选择器 / 文档。

运行：.venv/bin/python -m pytest tests/test_kimi_config.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _kimi():
    from ai_web2api.config import load_config

    return next(p for p in load_config(ROOT / "config.yaml").providers if p.name == "kimi")


def test_driver_registered():
    from ai_web2api.providers.kimi import KimiProvider
    from ai_web2api.providers.registry import DRIVERS
    from ai_web2api.providers.webchat import WebChatProvider

    assert DRIVERS["kimi"] is KimiProvider
    assert issubclass(KimiProvider, WebChatProvider)


def test_disabled_by_default_and_manual_login():
    k = _kimi()
    assert k.enabled is False, "Kimi 选择器待校准，必须默认禁用"
    assert k.url.startswith("https://www.kimi.com")
    assert k.login.mode == "manual", "Kimi 只有扫码/短信登录（带易盾验证码），无法自动化"
    assert "login" in k.login.hint


def test_measured_selectors():
    """实测过的选择器必须保留（未实测的允许调，但别把实测项删掉）。"""
    s = _kimi().selectors
    assert ".chat-input-editor" in s.input
    assert s.type_prompt is True, "contenteditable 需逐字输入"
    assert ".send-button-container" in s.send_button
    assert "input.hidden-input[type='file']" in s.upload_input


def test_doc_and_readme_mention_kimi():
    doc = ROOT / "docs" / "PROVIDER_KIMI.md"
    assert doc.is_file()
    body = doc.read_text(encoding="utf-8")
    assert "校准" in body and "response_container" in body, "文档必须给出校准步骤"
    for name in ("README.md", "README.zh-CN.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "Kimi" in text and "docs/PROVIDER_KIMI.md" in text, name


def test_provider_requires_calibration_marker():
    """配置里必须显式标注待校准项，避免后人以为已可用。"""
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
    block = cfg[cfg.index("  kimi:"):]
    block = block[: block.index("\n  # ", 10)] if "\n  # " in block[10:] else block
    assert "待实测" in block


def test_enabling_kimi_registers_provider_and_exposes_model():
    """enabled: true 时能正常注册并暴露模型（不需要真浏览器：registry 构造不启动页面）。"""
    import copy
    import yaml

    from ai_web2api.config import AppConfig
    from ai_web2api.providers.registry import DRIVERS, ProviderRegistry

    class _StubBrowser:
        default_locale = "zh-CN"

    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["providers"] = {"kimi": copy.deepcopy(raw["providers"]["kimi"])}
    raw["providers"]["kimi"]["enabled"] = True          # 后写入，覆盖配置文件里的 false
    cfg = AppConfig.model_validate(raw)
    assert DRIVERS["kimi"](cfg.providers[0], _StubBrowser()).name == "kimi"  # type: ignore[arg-type]
    reg = ProviderRegistry(cfg, _StubBrowser())  # type: ignore[arg-type]
    assert "kimi" in reg.providers(), "enabled=true 时 kimi 应被注册"
    assert "kimi-web" in reg.get_provider("kimi").exposed_models
