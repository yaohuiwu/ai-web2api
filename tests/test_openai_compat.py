"""用官方 openai SDK 对 ai-web2api 做端到端测试（假页，无需登录）。

覆盖主要功能：非流式/流式、模型列表、模型别名、未知模型 404、
thread_id 会话绑定（create/resume）、三模式 + 深度思考/智能搜索开关、
未知 mode 400、附件上传（image_url）、附件数量超限 400。

运行：.venv/bin/python -m pytest tests/test_openai_compat.py -v
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from openai import BadRequestError, NotFoundError, OpenAI
from typing import cast

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "bin" / "python"
FAKE_CONFIG = ROOT / "config.fake.yaml"

PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
           "AAAABJRU5ErkJggg==")
DATA_URL = "data:image/png;base64," + PNG_B64


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
    """起一个假页服务（随机端口，临时 config），返回指向它的 openai 客户端。"""
    port = _free_port()
    tmp_cfg = ROOT / "tests" / f".tmp_fake_{port}.yaml"
    tmp_cfg.write_text(
        re.sub(r"port: \d+", f"port: {port}", FAKE_CONFIG.read_text(), count=1)
    )
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


def _state_tags(content: str) -> dict:
    """解析假页回显的 [模式:X][深度思考:开/关][智能搜索:开/关][附件:...] 标签。"""
    tags: dict[str, str] = {}
    for m in re.finditer(r"\[(模式|深度思考|智能搜索|附件):([^\]]+)\]", content):
        tags[m.group(1)] = m.group(2)
    return tags


def _content(resp) -> str:
    """assistant 消息内容（SDK 类型为 str | None，测试里按 str 用）。"""
    return resp.choices[0].message.content or ""


def _thread_id(resp) -> str | None:
    """服务端自定义回显字段（SDK 静态类型未声明，运行时经 model_extra 可达）。"""
    return getattr(resp, "thread_id", None)


def _error_type(exc: BadRequestError) -> str:
    """错误响应 body 的 error.type（openai SDK 不同版本 body 结构不同：{"error":{...}} 或扁平）。"""
    body = cast(dict, exc.body or {})
    err = body.get("error") or body
    return cast(dict, err).get("type", "")


# ---------- 基础对话 ----------

def test_chat_basic(client: OpenAI):
    resp = client.chat.completions.create(
        model="fake-web",
        messages=[{"role": "user", "content": "你好"}],
    )
    assert resp.object == "chat.completion"
    assert resp.choices[0].message.role == "assistant"
    assert "好的" in _content(resp)
    tags = _state_tags(_content(resp))
    assert tags["模式"] == "快速模式"  # 默认模式


def test_chat_stream(client: OpenAI):
    stream = client.chat.completions.create(
        model="fake-web",
        messages=[{"role": "user", "content": "流式测试"}],
        stream=True,
    )
    chunks = [chunk.choices[0].delta.content or "" for chunk in stream]
    full = "".join(chunks)
    assert len(chunks) > 1  # 确实是分块流式
    assert "好的" in full
    assert "[模式:快速模式]" in full


def test_chat_multi_turn_history(client: OpenAI):
    """无状态多轮：同一请求内多条消息按顺序注入（模型看到历史）。"""
    resp = client.chat.completions.create(
        model="fake-web",
        messages=[
            {"role": "user", "content": "我叫小明"},
            {"role": "assistant", "content": "你好小明"},
            {"role": "user", "content": "我叫什么？"},
        ],
    )
    assert "好的" in resp.choices[0].message.content


# ---------- 模型 ----------

def test_models_list(client: OpenAI):
    models = client.models.list()
    ids = {m.id for m in models.data}
    assert {"fake-web", "fake-r1"} <= ids


def test_model_alias(client: OpenAI):
    """OpenAI 常见模型名（gpt-4 等）自动映射到 provider 默认模型。"""
    resp = client.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": "别名测试"}],
    )
    assert "好的" in _content(resp)
    assert resp.model == "gpt-4"  # 响应回显请求名（OpenAI 约定）


def test_unknown_model_404(client: OpenAI):
    with pytest.raises(NotFoundError) as ei:
        client.chat.completions.create(
            model="no-such-model",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert ei.value.status_code == 404


# ---------- thread_id 会话绑定 ----------

def test_thread_binding(client: OpenAI):
    tid = "sdk-thread-1"
    r1 = client.chat.completions.create(
        model="fake-web",
        messages=[{"role": "user", "content": "记住：我的代号是 Alpha"}],
        extra_body={"thread_id": tid},
    )
    assert _thread_id(r1) == tid  # 回显客户端 thread_id
    assert "好的" in _content(r1)

    # resume：只发最后一条 user 消息，页面持有历史
    r2 = client.chat.completions.create(
        model="fake-web",
        messages=[{"role": "user", "content": "我的代号是什么"}],
        extra_body={"thread_id": tid},
    )
    assert _thread_id(r2) == tid
    assert "好的" in _content(r2)


def test_thread_id_header(client: OpenAI):
    """X-Thread-Id header 方式绑定。"""
    tid = "sdk-thread-hdr"
    r = client.chat.completions.create(
        model="fake-web",
        messages=[{"role": "user", "content": "header 绑定"}],
        extra_headers={"X-Thread-Id": tid},
    )
    assert _thread_id(r) == tid


# ---------- 三模式 + 开关 ----------

def test_mode_image(client: OpenAI):
    resp = client.chat.completions.create(
        model="fake-web",
        messages=[{"role": "user", "content": "识图模式"}],
        extra_body={"mode": "image"},
    )
    tags = _state_tags(_content(resp))
    assert tags["模式"] == "识图模式"


def test_mode_expert_and_toggles(client: OpenAI):
    resp = client.chat.completions.create(
        model="fake-web",
        messages=[{"role": "user", "content": "专家模式"}],
        extra_body={"mode": "expert", "deep_think": True, "search": False},
    )
    tags = _state_tags(_content(resp))
    assert tags["模式"] == "专家模式"
    assert tags["深度思考"] == "开"
    assert tags["智能搜索"] == "关"


def test_toggles_default(client: OpenAI):
    """不传开关参数 → 页面现状（智能搜索默认开、深度思考默认关）。"""
    resp = client.chat.completions.create(
        model="fake-web",
        messages=[{"role": "user", "content": "开关默认态"}],
    )
    tags = _state_tags(_content(resp))
    assert tags["深度思考"] == "关"
    assert tags["智能搜索"] == "开"


def test_unknown_mode_400(client: OpenAI):
    with pytest.raises(BadRequestError) as ei:
        client.chat.completions.create(
            model="fake-web",
            messages=[{"role": "user", "content": "hi"}],
            extra_body={"mode": "bogus-mode"},
        )
    assert ei.value.status_code == 400
    assert _error_type(ei.value) == "unsupported_mode"


# ---------- 附件 ----------

def test_attachment_image_url(client: OpenAI):
    resp = client.chat.completions.create(
        model="fake-web",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "图里有什么"},
                {"type": "image_url", "image_url": {"url": DATA_URL}},
            ],
        }],
    )
    tags = _state_tags(_content(resp))
    assert tags["附件"] == "image.png"  # 原始文件名（临时文件保留原名）


def test_attachment_multi(client: OpenAI):
    resp = client.chat.completions.create(
        model="fake-web",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "两张图"},
                {"type": "image_url", "image_url": {"url": DATA_URL}},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + PNG_B64}},
            ],
        }],
    )
    tags = _state_tags(_content(resp))
    assert tags["附件"] == "image.png,image.jpg"


def test_attachment_limit_400(client: OpenAI):
    """超过 50 个 → 400 attachments_error（与 Web tooltips 一致）。"""
    parts = [{"type": "image_url", "image_url": {"url": DATA_URL}} for _ in range(51)]
    with pytest.raises(BadRequestError) as ei:
        client.chat.completions.create(
            model="fake-web",
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}] + parts}],
        )
    assert ei.value.status_code == 400
    assert _error_type(ei.value) == "attachments_error"


# ---------- 流式 + 附件组合 ----------

def test_stream_with_attachment(client: OpenAI):
    stream = client.chat.completions.create(
        model="fake-web",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": DATA_URL}},
            ],
        }],
        stream=True,
    )
    full = "".join(chunk.choices[0].delta.content or "" for chunk in stream)
    assert "[附件:image.png]" in full
