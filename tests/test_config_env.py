"""配置层单元测试：环境变量覆盖（DEEPSEEK_HEADLESS 之类）+ 新版 UI 模式预设。

运行：.venv/bin/python -m pytest tests/test_config_env.py tests/test_mode_presets.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_web2api.config import AppConfig, apply_env_overrides, load_config

ROOT = Path(__file__).resolve().parent.parent


def _cfg(headless: bool = False) -> AppConfig:
    return AppConfig.model_validate(
        {
            "browser": {"headless": headless},
            "providers": [{"name": "deepseek", "url": "https://example.com", "models": [{"name": "m"}]}],
        }
    )


def test_provider_env_overrides_yaml(monkeypatch: pytest.MonkeyPatch):
    """DEEPSEEK_HEADLESS=true 必须覆盖 config.yaml 的 headless: false（此前被忽略 → 仍弹窗口）。"""
    monkeypatch.setenv("DEEPSEEK_HEADLESS", "true")
    assert apply_env_overrides(_cfg(headless=False)).browser.headless is True


def test_provider_env_false_brings_back_window(monkeypatch: pytest.MonkeyPatch):
    """反向也生效：需要手动登录时 DEEPSEEK_HEADLESS=false 覆盖 YAML 的 true。"""
    monkeypatch.setenv("DEEPSEEK_HEADLESS", "false")
    assert apply_env_overrides(_cfg(headless=True)).browser.headless is False


def test_generic_env_wins_over_provider_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEEPSEEK_HEADLESS", "false")
    monkeypatch.setenv("WEB2API_HEADLESS", "1")
    assert apply_env_overrides(_cfg(headless=False)).browser.headless is True


def test_bad_env_value_is_ignored(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEEPSEEK_HEADLESS", "maybe")
    assert apply_env_overrides(_cfg(headless=False)).browser.headless is False


def test_no_env_keeps_yaml(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DEEPSEEK_HEADLESS", raising=False)
    monkeypatch.delenv("WEB2API_HEADLESS", raising=False)
    assert apply_env_overrides(_cfg(headless=False)).browser.headless is False


def test_real_config_loads_and_env_applies(monkeypatch: pytest.MonkeyPatch):
    """真 config.yaml：DEEPSEEK_HEADLESS=true 生效，且 locale 固定 zh-CN（中文选择器前提）。"""
    monkeypatch.setenv("DEEPSEEK_HEADLESS", "true")
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.browser.headless is True
    assert cfg.browser.locale == "zh-CN"
    # 新版 UI 无模式区 → mode_button 必须为空（mode 走开关预设路径）
    deepseek = next(p for p in cfg.providers if p.name == "deepseek")
    assert deepseek.selectors.mode_button == {}
    assert deepseek.selectors.toggle_button["deep_think"]
