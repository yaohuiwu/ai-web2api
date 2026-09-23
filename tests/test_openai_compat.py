"""用官方 openai SDK 对**已启动的** ai-web2api 服务做真实测试（真浏览器 + 真登录 DeepSeek）。

假设服务已在运行（默认 http://127.0.0.1:8000，可用 AI_WEB2API_BASE_URL 覆盖）：
    .venv/bin/python -m ai_web2api.main          # 另开一个终端先把服务跑起来
    .venv/bin/python -m pytest tests/test_openai_compat.py -v

覆盖：/v1/models、非流式/流式对话、多轮历史、模型别名、未知模型 404、thread_id 绑定
（create/resume 能回忆上下文、X-Thread-Id header）、深度思考开关（开 → reasoning_content，
关 → 无）、mode 预设（expert → 思考开）、未知 mode 不再 400、附件上传（data URL，
含多附件与 51 个超限 400）、流式 + 附件组合。

两点说明：
- 真模型输出不可精确断言 → 只断言结构与可判定的语义；模型偶发空回复时重试一次。
- 每个用例结束会 DELETE 掉本用例新建的会话（不影响你原有的会话与侧栏条目），
  否则无状态请求会各自占一个 thread，撞上 max_threads=8 后全是 429。
- 服务未启动 / 未登录 → 整个模块 skip（不是失败）。
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import urllib.request
from pathlib import Path
from typing import Iterator, cast

import pytest
from openai import BadRequestError, InternalServerError, NotFoundError, OpenAI

BASE = os.getenv("AI_WEB2API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
MODEL = "deepseek-web"

# 题目/关键字一律**随机生成**：固定重复文本是自动化特征（实测会被站点风控盯上）。
# 复现用 TEXT_FIDELITY_SEED=42；题目库在 tests/prompts/corpus.yaml。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompt_pool import SHORT, sample_one, seed_from_env  # noqa: E402

_rng = random.Random(seed_from_env())
QUICK = sample_one(SHORT, seed=seed_from_env())        # 短提示词，减少等待
THINK_PROMPT = sample_one(SHORT, seed=seed_from_env())  # 要求"先简短思考"再回答
SECRET_NUM = _rng.randint(10, 99)                       # 记忆类用例的随机数字
CODENAME = _rng.choice(["Alpha", "Bravo", "Delta", "Omega", "Sigma", "Nova", "Zulu"])

PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQ"
           "AAAABJRU5ErkJggg==")
PNG2_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9"
            "awAAAABJRU5ErkJggg==")   # 另一张 1x1（黑），多附件用例用
DATA_URL = "data:image/png;base64," + PNG_B64
DATA_URL_2 = "data:image/png;base64," + PNG2_B64

pytestmark = pytest.mark.live  # 需要已启动且已登录的服务，默认排除


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as r:
        return cast(dict, json.loads(r.read()))


def _delete_thread(tid: str) -> None:
    req = urllib.request.Request(f"{BASE}/admin/threads/{tid}", method="DELETE")
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception:  # noqa: BLE001  # 清理失败不影响用例结论
        pass


@pytest.fixture(scope="module")
def client() -> OpenAI:
    """连到已启动的服务；连不上或未登录 → skip（含清晰的启动提示）。"""
    try:
        _get("/healthz")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"服务未启动（{BASE}）：{exc}；先跑 .venv/bin/python -m ai_web2api.main")
    providers = _get("/admin/status").get("providers") or []
    if not any(p.get("logged_in") for p in providers):
        pytest.skip("DeepSeek 未登录：POST /admin/deepseek/login/start（或配好 .env 凭据后 login/auto）")
    return OpenAI(base_url=f"{BASE}/v1", api_key="unused")


def _thread_ids() -> set[str]:
    """当前会话列表（服务不可用时返回空集，交给 client fixture 去 skip）。"""
    try:
        return {t["thread_id"] for t in _get("/admin/threads").get("threads", [])}
    except Exception:  # noqa: BLE001
        return set()


@pytest.fixture(autouse=True)
def _reap_new_threads() -> Iterator[None]:
    """用例新建的会话用完即删：不占 max_threads 名额，也不长期留在侧栏。"""
    before = _thread_ids()
    yield
    for tid in sorted(_thread_ids() - before):
        _delete_thread(tid)


# ---------- 断言助手 ----------

def _content(resp) -> str:
    """assistant 消息内容（SDK 类型为 str | None）。"""
    return resp.choices[0].message.content or ""


def _reasoning(resp) -> str:
    """思考内容（服务端自定义字段，运行时经 model_extra 可达）。"""
    msg = resp.choices[0].message
    return cast(str, (msg.model_extra or {}).get("reasoning_content") or "")


def _thread_id(resp) -> str | None:
    return getattr(resp, "thread_id", None)


def _error_type(exc: BadRequestError) -> str:
    """错误响应 body 的 error.type（SDK 各版本结构不同：{"error":{...}} 或扁平）。"""
    body = cast(dict, exc.body or {})
    err = body.get("error") or body
    return cast(dict, err).get("type", "")


def _call(client: OpenAI, **kwargs):
    """发请求；503（未登录 / 页面临时打不开）视为环境问题 → skip，不报假失败。"""
    try:
        return client.chat.completions.create(**kwargs)
    except InternalServerError as exc:
        text = str(exc)
        if "not_logged_in" in text or "page_unavailable" in text:
            pytest.skip(f"环境不可用（{text.split('{')[0].strip()[:160]}）")
        raise


def _create(client: OpenAI, **kwargs):
    """真实模型偶发空回复（finish_reason=stop 且 content 为空）→ 重试一次。"""
    resp = _call(client, **kwargs)
    if not _content(resp).strip():
        resp = _call(client, **kwargs)
    return resp


def _create_expect_reasoning(client: OpenAI, **kwargs):
    """要求返回 reasoning_content；真模型偶发不思考 → 重试一次。"""
    resp = _call(client, **kwargs)
    if not _reasoning(resp).strip():
        resp = _call(client, **kwargs)
    return resp


def _stream_text(client: OpenAI, **kwargs) -> str:
    """流式收集文本；真模型偶发空流 → 重试一次。"""
    def once() -> str:
        stream = _call(client, **kwargs)
        return "".join(c.choices[0].delta.content or "" for c in stream)

    text = once()
    return text if text.strip() else once()


# ---------- 基础对话 ----------

def test_chat_basic(client: OpenAI):
    """非流式：真实回复有内容，OpenAI 结构正确。"""
    resp = _create(
        client,
        model=MODEL,
        messages=[{"role": "user", "content": QUICK}],
        extra_body={"deep_think": False, "search": False},
    )
    assert resp.object == "chat.completion"
    assert resp.choices[0].message.role == "assistant"
    assert _content(resp).strip(), "回复为空"


def test_chat_stream(client: OpenAI):
    """流式：多块增量输出，拼接后非空。"""
    full = _stream_text(
        client,
        model=MODEL,
        messages=[{"role": "user", "content": QUICK}],
        extra_body={"deep_think": False, "search": False},
        stream=True,
    )
    assert full.strip(), "流式拼接为空"


def test_chat_multi_turn_history(client: OpenAI):
    """无状态多轮：同一请求内多条消息按顺序注入（模型能看到历史）。"""
    resp = _create(
        client,
        model=MODEL,
        messages=[
            {"role": "user", "content": f"记住数字 {SECRET_NUM}。"},
            {"role": "assistant", "content": "好的。"},
            {"role": "user", "content": "我刚让你记住的数字是几？只回复数字"},
        ],
        extra_body={"deep_think": False, "search": False},
    )
    assert "7" in _content(resp), f"未看到历史中的数字：{_content(resp)[:80]!r}"


# ---------- 模型 ----------

def test_models_list(client: OpenAI):
    ids = {m.id for m in client.models.list().data}
    assert MODEL in ids


def test_model_alias(client: OpenAI):
    """OpenAI 常见模型名（gpt-4 等）映射到 provider 默认模型。"""
    resp = _create(
        client,
        model="gpt-4",
        messages=[{"role": "user", "content": QUICK}],
        extra_body={"deep_think": False, "search": False},
    )
    assert _content(resp).strip()
    assert resp.model == "gpt-4"  # 响应回显请求名（OpenAI 约定）


def test_unknown_model_404(client: OpenAI):
    with pytest.raises(NotFoundError) as ei:
        client.chat.completions.create(model="no-such-model", messages=[{"role": "user", "content": "hi"}])
    assert ei.value.status_code == 404


# ---------- thread_id 会话绑定 ----------

def test_thread_binding(client: OpenAI):
    """create + resume：同一 thread 复用同一 Web 会话（能回忆起上一轮）。"""
    tid = "it-thread-1"
    r1 = _create(
        client,
        model=MODEL,
        messages=[{"role": "user", "content": f"记住：我的代号是 {CODENAME}。"}],
        extra_body={"thread_id": tid, "deep_think": False, "search": False},
    )
    assert _thread_id(r1) == tid
    assert _content(r1).strip()

    # resume：服务端只发最后一条 user，页面持有历史
    r2 = _call(
        client,
        model=MODEL,
        messages=[
            {"role": "user", "content": f"记住：我的代号是 {CODENAME}。"},
            {"role": "user", "content": "我的代号是什么？只回复代号"},
        ],
        extra_body={"thread_id": tid, "deep_think": False, "search": False},
    )
    assert _thread_id(r2) == tid
    assert "Alpha" in _content(r2), f"未复用会话上下文：{_content(r2)[:80]!r}"


def test_thread_id_header(client: OpenAI):
    """X-Thread-Id header 方式绑定。"""
    tid = "it-thread-hdr"
    r = _call(
        client,
        model=MODEL,
        messages=[{"role": "user", "content": QUICK}],
        extra_headers={"X-Thread-Id": tid},
        extra_body={"deep_think": False, "search": False},
    )
    assert _thread_id(r) == tid


# ---------- 深度思考开关（新版 UI 的唯一输出控制项）----------

def test_deep_think_on_produces_reasoning(client: OpenAI):
    """深度思考开 → 返回 reasoning_content（思考过程）。"""
    resp = _create_expect_reasoning(
        client,
        model=MODEL,
        messages=[{"role": "user", "content": THINK_PROMPT}],
        extra_body={"deep_think": True, "search": False},
    )
    assert _reasoning(resp).strip(), "深度思考=开，但未返回思考内容"


def test_deep_think_off_no_reasoning(client: OpenAI):
    """深度思考关 → 不返回 reasoning_content。"""
    resp = _create(
        client,
        model=MODEL,
        messages=[{"role": "user", "content": THINK_PROMPT}],
        extra_body={"deep_think": False, "search": False},
    )
    assert _reasoning(resp) == "", f"深度思考=关，却返回了思考内容：{_reasoning(resp)[:80]!r}"
    assert _content(resp).strip()


def test_mode_preset_expert(client: OpenAI):
    """旧 mode 值在新版 UI 翻译成开关组合（expert = 思考开 + 搜索开）。"""
    resp = _create_expect_reasoning(
        client,
        model=MODEL,
        messages=[{"role": "user", "content": THINK_PROMPT}],
        extra_body={"mode": "expert"},
    )
    assert _reasoning(resp).strip(), "mode=expert 未开启深度思考"


def test_unknown_mode_ignored(client: OpenAI):
    """新版 UI 已无模式区：未知 mode 只记日志，不再 400。"""
    resp = _create(
        client,
        model=MODEL,
        messages=[{"role": "user", "content": QUICK}],
        extra_body={"mode": "bogus-mode", "deep_think": False, "search": False},
    )
    assert _content(resp).strip()


# ---------- 附件 ----------

def test_attachment_image_url(client: OpenAI):
    """data URL → 临时文件（保留原名）→ 上传到输入框，模型能看到图。"""
    resp = _create(
        client,
        model=MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": "这张图是什么颜色？只回复一个词"},
                {"type": "image_url", "image_url": {"url": DATA_URL}},
            ],
        }],
        extra_body={"deep_think": False, "search": False},
    )
    answer = _content(resp)
    assert answer.strip(), "看图后回复为空"
    assert re.search(r"红|粉|赤", answer), f"未识别出图片颜色：{answer[:60]!r}"


def test_attachment_multi(client: OpenAI):
    """多附件（两张图）一次上传。"""
    resp = _create(
        client,
        model=MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": QUICK},
                {"type": "image_url", "image_url": {"url": DATA_URL}},
                {"type": "image_url", "image_url": {"url": DATA_URL_2}},
            ],
        }],
        extra_body={"deep_think": False, "search": False},
    )
    assert _content(resp).strip()


def test_attachment_limit_400(client: OpenAI):
    """超过 50 个 → 400 attachments_error（与 Web tooltip 一致；开浏览器之前就拒绝）。"""
    parts = [{"type": "image_url", "image_url": {"url": DATA_URL}} for _ in range(51)]
    with pytest.raises(BadRequestError) as ei:
        client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}] + parts}],
        )
    assert ei.value.status_code == 400
    assert _error_type(ei.value) == "attachments_error"


# ---------- 流式 + 附件组合 ----------

def test_stream_with_attachment(client: OpenAI):
    full = _stream_text(
        client,
        model=MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": QUICK},
                {"type": "image_url", "image_url": {"url": DATA_URL}},
            ],
        }],
        extra_body={"deep_think": False, "search": False},
        stream=True,
    )
    assert full.strip(), "流式 + 附件无输出"
