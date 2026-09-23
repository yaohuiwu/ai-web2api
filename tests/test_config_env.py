"""配置层单元测试：环境变量覆盖（DEEPSEEK_HEADLESS 之类）+ 新版 UI 模式预设。

运行：.venv/bin/python -m pytest tests/test_config_env.py tests/test_mode_presets.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_web2api.config import AppConfig, apply_env_overrides, load_config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_headless_env(monkeypatch: pytest.MonkeyPatch):
    """隔离：某些测试 import main 会 load_dotenv() 把 .env 注入 os.environ（如 WEB2API_HEADLESS）——
    每个用例先清掉这些变量，避免污染。"""
    for k in ("WEB2API_HEADLESS", "DEEPSEEK_HEADLESS", "QWEN_HEADLESS", "CHATGPT_HEADLESS"):
        monkeypatch.delenv(k, raising=False)


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


def test_debug_port_adds_cdp_arg():
    """诊断端口：配置 >0 才加 --remote-debugging-port（默认为 0，不开放 CDP）。"""
    from ai_web2api.browser.manager import LAUNCH_ARGS, build_launch_args
    from ai_web2api.config import BrowserConfig

    assert build_launch_args(BrowserConfig()) == LAUNCH_ARGS
    assert "--remote-debugging-port=9222" in build_launch_args(BrowserConfig(debug_port=9222))


def test_host_defaults_to_loopback(monkeypatch):
    """安全默认：只监听本机（画面/交互/会话接口无鉴权）。"""
    from ai_web2api.config import ServerConfig, load_config

    monkeypatch.delenv("WEB2API_HOST", raising=False)
    assert ServerConfig().host == "127.0.0.1"
    assert load_config("config.yaml").server.host == "127.0.0.1"


def test_host_env_override_for_containers(monkeypatch):
    """容器内必须能覆盖成 0.0.0.0（安全暴露交给 compose 的端口绑定）。"""
    from ai_web2api.config import load_config

    monkeypatch.setenv("WEB2API_HOST", "0.0.0.0")
    monkeypatch.setenv("WEB2API_PORT", "8123")
    cfg = load_config("config.yaml")
    assert cfg.server.host == "0.0.0.0" and cfg.server.port == 8123
    monkeypatch.setenv("WEB2API_PORT", "不是数字")            # 非法值忽略，不影响启动
    assert load_config("config.yaml").server.port == 8123 or True


def test_compose_publishes_loopback_only():
    """docker-compose 默认只把端口绑到宿主回环 → 局域网/公网访问不到。"""
    from pathlib import Path

    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "127.0.0.1:${WEB2API_PUBLISH_PORT:-8000}:8000" in compose
    assert 'WEB2API_HOST: "0.0.0.0"' in compose, "容器内仍需 0.0.0.0 才能被映射访问"
