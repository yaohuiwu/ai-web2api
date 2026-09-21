"""历史消息落库 + GET /admin/threads/{id}/messages 端到端（假页，无需登录）。

起一个假页服务（config.fake.yaml 派生，thread_persist=true），发两轮对话，
断言历史接口返回完整的 user/assistant（含思考）。

运行：.venv/bin/python -m pytest tests/test_history_endpoint.py -v
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "bin" / "python"
FAKE_CONFIG = ROOT / "config.fake.yaml"


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


def _post_chat(base: str, thread_id: str, text: str) -> dict:
    body = json.dumps(
        {
            "model": "fake-web",
            "messages": [{"role": "user", "content": text}],
            "stream": False,
            "thread_id": thread_id,
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base}{path}", timeout=10) as r:
        return json.loads(r.read())


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory):
    port = _free_port()
    cfg = yaml.safe_load(FAKE_CONFIG.read_text())
    cfg["server"]["port"] = port
    cfg["server"]["thread_persist"] = True  # 本测试要验证落库
    profiles = tmp_path_factory.mktemp("profiles")
    cfg["profiles_dir"] = str(profiles)
    # 临时配置在 tmp 下：URL 用绝对路径指向仓库假页
    cfg["providers"]["fake"]["url"] = f"file://{ROOT / 'tests' / 'fake_chat.html'}"
    tmp_cfg = profiles / "config.yaml"
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
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_messages_persisted_and_resumed(base_url: str):
    tid = "hist-test-1"

    first = _post_chat(base_url, tid, "第一轮")
    assert first["thread_id"] == tid
    first_reply = first["choices"][0]["message"]["content"]
    assert first_reply  # 非空回复

    msgs = _get(base_url, f"/admin/threads/{tid}/messages")["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "第一轮"
    assert msgs[1]["content"] == first_reply

    # 第二轮（resume）→ 历史累加到 4 条
    second = _post_chat(base_url, tid, "第二轮")
    second_reply = second["choices"][0]["message"]["content"]
    assert second_reply

    msgs = _get(base_url, f"/admin/threads/{tid}/messages")["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[2]["content"] == "第二轮"
    assert msgs[3]["content"] == second_reply


def test_delete_thread_drops_history(base_url: str):
    tid = "hist-test-del"
    _post_chat(base_url, tid, "会被删掉")
    assert _get(base_url, f"/admin/threads/{tid}/messages")["messages"]

    req = urllib.request.Request(f"{base_url}/admin/threads/{tid}", method="DELETE")
    urllib.request.urlopen(req, timeout=10).close()

    assert _get(base_url, f"/admin/threads/{tid}/messages")["messages"] == []
    assert all(t["thread_id"] != tid for t in _get(base_url, "/admin/threads")["threads"])
