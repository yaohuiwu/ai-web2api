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


# ---------- B1：交互选择 / providers 子命令 / 导入地址 ----------


def _cfg(raw: dict) -> AppConfig:
    return AppConfig.model_validate(raw)


def test_providers_subcommand_parsed():
    a = build_parser().parse_args(["providers"])
    assert a.cmd == "providers"


def test_import_url_env_default(monkeypatch):
    monkeypatch.setenv("AI_WEB2API_URL", "http://example.test:9999")
    a = build_parser().parse_args(["login", "qwen"])
    assert a.import_url == "http://example.test:9999"
    a2 = build_parser().parse_args(["login", "qwen", "--import-url", "http://x:1"])
    assert a2.import_url == "http://x:1"  # 显式优先


def test_pick_provider_given_and_invalid():
    from ai_web2api.cli import _pick_provider

    cfg = _cfg({"server": {"port": 8000}, "profiles_dir": "/tmp/x", "providers": {}})
    assert _pick_provider(cfg, None, ["a", "b"], "a") == "a"       # 指定
    assert _pick_provider(cfg, None, ["a", "b"], "zzz") is None    # 未知 → None
    assert _pick_provider(cfg, None, [], None) is None             # 无可用


def test_pick_provider_non_tty_uses_default(monkeypatch):
    import sys as _sys
    from ai_web2api.cli import _pick_provider

    monkeypatch.setattr(_sys, "stdin", type("S", (), {"isatty": lambda self: False})())
    cfg = _cfg({"server": {"port": 8000}, "profiles_dir": "/tmp/x", "providers": {}})
    assert _pick_provider(cfg, None, ["a", "b"], None) == "a"  # 非 TTY → 默认第一个


def test_cmd_providers_lists_expiry(tmp_path, capsys):
    import json
    import time

    from ai_web2api.cli import _cmd_providers

    raw = {
        "profiles_dir": str(tmp_path),
        "providers": {
            "fake": {
                "driver": "deepseek",
                "url": "file://./tests/fake_chat.html",
                "models": [{"name": "fake-web"}],
                "login": {"mode": "manual", "auth_cookies": ["token"]},
            }
        },
    }
    cfg = _cfg(raw)
    d = tmp_path / "fake"
    d.mkdir(parents=True)
    (d / "state.json").write_text(
        json.dumps({"cookies": [{"name": "token", "value": "x", "expires": time.time() + 10 * 86400}]}),
        encoding="utf-8",
    )
    assert _cmd_providers(cfg) == 0
    out = capsys.readouterr().out
    assert "fake" in out and "manual" in out and "还剩 10 天" in out


def test_console_script_declared():
    """`pip install -e .` 后应能直接用 `ai-web2api login`。"""
    from pathlib import Path

    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert "[project.scripts]" in pyproject
    assert 'ai-web2api = "ai_web2api.cli:main"' in pyproject
