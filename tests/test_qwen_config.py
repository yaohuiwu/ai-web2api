"""Qwen provider 配置与注册（不联网）。

运行：.venv/bin/python -m pytest tests/test_qwen_config.py -v
"""

from __future__ import annotations

from pathlib import Path

from ai_web2api.config import load_config
from ai_web2api.providers.qwen import QwenProvider
from ai_web2api.providers.registry import DRIVERS

ROOT = Path(__file__).resolve().parent.parent


def test_qwen_driver_registered():
    assert DRIVERS.get("qwen") is QwenProvider
    assert issubclass(QwenProvider, object)


def test_qwen_config_valid():
    cfg = load_config(ROOT / "config.yaml")
    names = [p.name for p in cfg.providers]
    assert "deepseek" in names and "qwen" in names

    q = next(p for p in cfg.providers if p.name == "qwen")
    assert q.driver == "qwen"
    assert q.url.rstrip("/") == "https://chat.qwen.ai"
    assert q.login.url == "https://chat.qwen.ai/auth"
    assert (q.login.username_env, q.login.password_env) == ("QWEN_USERNAME", "QWEN_PASSWORD")
    # 下拉菜单配置
    assert q.selectors.model_menu.trigger
    assert q.selectors.mode_menu.labels == {"auto": "自动", "thinking": "思考", "fast": "快速"}
    assert q.selectors.attachment_menu.file_input == ["input#filesUpload"]
    # 模型对外名统一 -web 后缀
    assert q.models and all(m.name.endswith("-web") for m in q.models)
