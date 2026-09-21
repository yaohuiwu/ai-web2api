"""网络抓取框架：按 network.url_pattern 生成 XHR 监听脚本。

运行：.venv/bin/python -m pytest tests/test_network_capture.py -v
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile

import pytest

from ai_web2api.config import ProviderConfig
from ai_web2api.providers.webchat import WebChatProvider


class _StubBrowser:
    default_locale = "zh-CN"


def _prov(**extra) -> WebChatProvider:
    cfg = ProviderConfig.model_validate(
        {
            "name": "qwen",
            "url": "https://chat.qwen.ai/",
            "models": [{"name": "qwen-max"}],
            **extra,
        }
    )
    return WebChatProvider(cfg, _StubBrowser())  # type: ignore[arg-type]


def test_no_network_config_no_scripts():
    assert _prov().init_scripts() == []
    assert _prov().net_enabled is False
    assert _prov(network={"capture": False, "url_pattern": "/x"}).init_scripts() == []


def test_generates_capture_script():
    p = _prov(network={"url_pattern": "/api/v0/chat/completion"})
    assert p.net_enabled is True
    scripts = p.init_scripts()
    assert len(scripts) == 1
    js = scripts[0]
    assert "XMLHttpRequest" in js
    assert "__aiw2a_sse" in js
    assert "/api/v0/chat/completion" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 校验 JS 语法")
def test_generated_js_parses():
    js = _prov(network={"url_pattern": "/a/b\\d+"}).init_scripts()[0]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
        path = f.name
    r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
