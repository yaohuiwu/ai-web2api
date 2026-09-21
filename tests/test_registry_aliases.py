"""多 provider 别名归属与重复模型名。

运行：.venv/bin/python -m pytest tests/test_registry_aliases.py -v
"""

from __future__ import annotations

import pytest

from ai_web2api.config import AppConfig
from ai_web2api.core.errors import ModelNotFoundError
from ai_web2api.providers.registry import ProviderRegistry


def _cfg(default_provider: str | None = None) -> AppConfig:
    server = {"default_provider": default_provider} if default_provider else {}
    return AppConfig.model_validate(
        {
            "server": server,
            "providers": [
                {
                    "name": "deepseek",
                    "driver": "deepseek",
                    "url": "https://chat.deepseek.com",
                    "models": [{"name": "deepseek-web"}],
                },
                {
                    "name": "qwen",
                    "driver": "qwen",
                    "url": "https://chat.qwen.ai/",
                    "models": [{"name": "qwen3.7-plus-web"}],
                },
            ],
        }
    )


def test_default_provider_owns_common_aliases():
    reg = ProviderRegistry(_cfg("qwen"), browser=None)  # type: ignore[arg-type]
    assert reg.get_for_model("gpt-4").name == "qwen"
    assert reg.resolve_model_name("gpt-4") == "qwen3.7-plus-web"


def test_alias_falls_back_to_first_provider():
    reg = ProviderRegistry(_cfg(), browser=None)  # type: ignore[arg-type]
    assert reg.get_for_model("gpt-4").name == "deepseek"
    assert reg.resolve_model_name("gpt-4o") == "deepseek-web"


def test_default_provider_missing_no_alias():
    reg = ProviderRegistry(_cfg("nope"), browser=None)  # type: ignore[arg-type]
    with pytest.raises(ModelNotFoundError):
        reg.get_for_model("gpt-4")
    # 但各 provider 自有模型仍可用
    assert reg.get_for_model("qwen3.7-plus-web").name == "qwen"


def test_duplicate_model_name_last_wins(caplog):
    cfg = AppConfig.model_validate(
        {
            "providers": [
                {"name": "a", "driver": "deepseek", "url": "https://x", "models": [{"name": "m"}]},
                {"name": "b", "driver": "qwen", "url": "https://y", "models": [{"name": "m"}]},
            ]
        }
    )
    with caplog.at_level("WARNING"):
        reg = ProviderRegistry(cfg, browser=None)  # type: ignore[arg-type]
    assert reg.get_for_model("m").name == "b"
    assert any("重复" in r.message for r in caplog.records)


def test_login_status_debounce():
    """一次性检测失败不应立刻判未登录（Qwen 这类站点会瞬时误报）。"""
    reg = ProviderRegistry(_cfg(), browser=None)  # type: ignore[arg-type]
    reg.set_login_status("qwen", True)
    assert reg._update_status("qwen", False) is True  # 第 1 次失败 → 忽略
    assert reg.login_status()["qwen"] is True
    assert reg._update_status("qwen", False) is False  # 第 2 次失败 → 生效
    assert reg.login_status()["qwen"] is False
    assert reg._update_status("qwen", True) is True  # 恢复登录
    assert reg.login_status()["qwen"] is True
    assert reg._update_status("qwen", False) is True  # 防抖计数已重置


def test_login_status_seeded_from_state_file(tmp_path):
    """有 state.json 的 provider 初始视为已登录（避免首次检测不确定时误显示未登录）。"""
    from ai_web2api.browser.manager import BrowserManager
    from ai_web2api.config import BrowserConfig

    (tmp_path / "qwen").mkdir()
    (tmp_path / "qwen" / "state.json").write_text("{}", encoding="utf-8")
    bm = BrowserManager(BrowserConfig(), tmp_path)
    reg = ProviderRegistry(_cfg(), browser=bm)
    st = reg.login_status()
    assert st.get("qwen") is True
    assert not st.get("deepseek")  # 无 state 文件 → 不预置
