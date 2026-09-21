"""新版 UI（三模式合一）的 mode → 开关预设行为，端到端跑假页驱动。

旧版 UI 走 radio（见 test_openai_compat.py 的 test_mode_*）；
本文件把假配置的 mode_button 清空，模拟"页面没有模式区"的新版 DeepSeek：
mode 值应翻译成 深度思考/智能搜索 开关组合，未知 mode 不再 400。

运行：.venv/bin/python -m pytest tests/test_mode_presets.py -v
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
import yaml
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "bin" / "python"
FAKE_CONFIG = ROOT / "config.fake.yaml"

pytestmark = pytest.mark.slow  # 起 subprocess + Playwright 的 e2e，默认排除


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(base: str, timeout: float = 60) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


@pytest.fixture(scope="module")
def client() -> OpenAI:
    """假页服务（无模式区的新版 UI 形态）。"""
    port = _free_port()
    cfg = yaml.safe_load(FAKE_CONFIG.read_text())
    cfg["server"]["port"] = port
    # 临时配置写在 tests/ 下：URL 用绝对路径指向仓库假页（配置里的相对 file:// 是
    # 相对 config.fake.yaml 所在目录，复制到别处后不再成立）
    cfg["providers"]["fake"]["url"] = f"file://{ROOT / 'tests' / 'fake_chat.html'}"
    sel = cfg["providers"]["fake"]["selectors"]
    sel["mode_button"] = {}   # 新版 UI：无模式 radio
    sel["mode_checked"] = []
    tmp_cfg = ROOT / "tests" / f".tmp_unified_{port}.yaml"
    tmp_cfg.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    proc = subprocess.Popen(
        [str(PYTHON), "-m", "ai_web2api.main"],
        env={**os.environ, "AI_WEB2API_CONFIG": str(tmp_cfg)},
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        assert _wait_health(base), "假页服务未在超时内就绪"
        yield OpenAI(base_url=f"{base}/v1", api_key="unused")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        tmp_cfg.unlink(missing_ok=True)


def _tags(resp) -> dict:
    content = resp.choices[0].message.content or ""
    return {m.group(1): m.group(2) for m in re.finditer(r"\[(模式|深度思考|智能搜索):([^\]]+)\]", content)}


def test_mode_fast_turns_both_toggles_off(client: OpenAI):
    """mode=fast（快速模式）→ 新版 UI 翻译成 思考关 + 搜索关。"""
    resp = client.chat.completions.create(
        model="fake-web",
        messages=[{"role": "user", "content": "快速"}],
        extra_body={"mode": "fast"},
    )
    tags = _tags(resp)
    assert tags["深度思考"] == "关"
    assert tags["智能搜索"] == "关"


def test_mode_expert_turns_both_toggles_on(client: OpenAI):
    """mode=expert（专家模式）→ 思考开 + 搜索开。"""
    resp = client.chat.completions.create(
        model="fake-web",
        messages=[{"role": "user", "content": "专家"}],
        extra_body={"mode": "expert"},
    )
    tags = _tags(resp)
    assert tags["深度思考"] == "开"
    assert tags["智能搜索"] == "开"


def test_explicit_toggles_override_mode_preset(client: OpenAI):
    """显式 deep_think/search 优先于 mode 预设。"""
    resp = client.chat.completions.create(
        model="fake-web",
        messages=[{"role": "user", "content": "覆盖"}],
        extra_body={"mode": "fast", "deep_think": True, "search": True},
    )
    tags = _tags(resp)
    assert tags["深度思考"] == "开"
    assert tags["智能搜索"] == "开"


def test_unknown_mode_ignored_not_400(client: OpenAI):
    """新版 UI 没有模式可校验 → 未知 mode 只记日志，不再 400。"""
    resp = client.chat.completions.create(
        model="fake-web",
        messages=[{"role": "user", "content": "未知模式"}],
        extra_body={"mode": "bogus-mode"},
    )
    assert (resp.choices[0].message.content or "")  # 正常返回内容
