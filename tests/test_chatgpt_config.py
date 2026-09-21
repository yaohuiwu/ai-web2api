"""ChatGPT provider 配置与注册（不联网）。

运行：.venv/bin/python -m pytest tests/test_chatgpt_config.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

from ai_web2api.config import load_config
from ai_web2api.providers.chatgpt import ChatGPTProvider
from ai_web2api.providers.registry import DRIVERS

ROOT = Path(__file__).resolve().parent.parent


def test_chatgpt_driver_registered():
    assert DRIVERS.get("chatgpt") is ChatGPTProvider


def test_chatgpt_session_url_pattern():
    m = re.search(
        ChatGPTProvider.session_url_pattern or "",
        "https://chatgpt.com/c/WEB:8d21d588-1b2c-4052-9a1b-d3c47ab6434c",
    )
    assert m and m.group(1) == "WEB:8d21d588-1b2c-4052-9a1b-d3c47ab6434c"


def test_chatgpt_config_valid():
    cfg = load_config(ROOT / "config.yaml")
    assert "chatgpt" in [p.name for p in cfg.providers]
    c = next(p for p in cfg.providers if p.name == "chatgpt")
    assert c.driver == "chatgpt"
    assert c.url.rstrip("/") == "https://chatgpt.com"
    assert c.locale == "en-US"
    assert c.session_url == "{base}/c/{id}"
    assert c.login.mode == "manual"  # ChatGPT 无密码自动登录
    # contenteditable → 必须逐字输入
    assert c.selectors.type_prompt is True
    assert "#prompt-textarea" in c.selectors.input
    assert c.selectors.stop_button and "stop-button" in c.selectors.stop_button[0]
    assert c.selectors.response_container
    assert c.selectors.model_menu.trigger
    assert "#upload-files" in c.selectors.attachment_menu.file_input
    # 模型对外名统一 -web 后缀
    assert c.models and all(m.name.endswith("-web") for m in c.models)
