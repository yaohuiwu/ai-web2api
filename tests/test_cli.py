"""CLI（python -m ai_web2api.cli）：参数解析 + 纯函数单测。

网页交互流程不在此测（需真实浏览器）；这里覆盖参数解析与本地地址推导/导入请求。

运行：.venv/bin/python -m pytest tests/test_cli.py -v
"""

from __future__ import annotations

from ai_web2api.cli import _local_import_url, _post_state, build_parser
from ai_web2api.config import AppConfig


def test_login_parser_defaults():
    a = build_parser().parse_args(["login", "qwen"])
    assert a.cmd == "login" and a.provider == "qwen"
    assert a.manual is False and a.import_state is True
    assert a.timeout == 600.0


def test_login_parser_options():
    a = build_parser().parse_args(
        ["login", "deepseek", "--manual", "--no-import", "--timeout", "30", "--import-url", "http://x:1"]
    )
    assert a.manual is True and a.import_state is False
    assert a.timeout == 30.0 and a.import_url == "http://x:1"


def test_local_import_url_maps_wildcard():
    wildcard = AppConfig.model_validate({"server": {"host": "0.0.0.0", "port": 8000}})
    assert _local_import_url(wildcard) == "http://127.0.0.1:8000"
    fixed = AppConfig.model_validate({"server": {"host": "192.168.1.5", "port": 9001}})
    assert _local_import_url(fixed) == "http://192.168.1.5:9001"


def test_post_state_unreachable_returns_false():
    ok, msg = _post_state("http://127.0.0.1:1", "qwen", {"cookies": [{"name": "x", "value": "y"}]}, None)
    assert ok is False and msg
