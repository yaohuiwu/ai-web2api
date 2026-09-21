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
