"""HTTP 路由：OpenAI 兼容 API + admin 登录管理。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel

from ..browser import extractor
from ..browser.control import InputAction as _InputAction
from ..browser.control import apply_input as _apply_input
from ..browser.live import (
    MJPEG_BOUNDARY,
    LiveController,
    capture,
    mjpeg_part,
    parse_frame_options,
)
from ..core.auth_expiry import compute_for_state_file
from ..core.repo_info import repo_info
from ..core.timeline import TIMELINES
from ..core.widgets import WidgetStore
from ..core.errors import (
    ProviderError,
    RateLimitedError,
    ThreadBusyError,
    ThreadExpiredError,
    ThreadTimeoutError,
)
from ..core.threads import ThreadManager
from ..providers.base import last_user_message
from ..providers.registry import ProviderRegistry
from ..tool_calling.converter import (
    build_prompt as fc_build_prompt,
)
from ..tool_calling.converter import (
    build_resume_prompt as fc_build_resume_prompt,
)
from ..tool_calling.converter import needs_tool_handling, parse_tool_response
from .schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    CookiesPayload,
    ResponseMessage,
    StorageStatePayload,
    normalize_message,
    LiveInputRequest,
)

logger = logging.getLogger(__name__)


class ProbePayload(BaseModel):
    text: str = "你好"
    wait_ms: int = 3000


# probe：等待"输入框被清空/回复区出现变化"（简单起见：等待文本出现在消息区）
_PROBE_WAIT_JS = """
(args) => {
  const el = document.querySelector(args.inputSel);
  return !!el && el.value === "";
}
"""

# probe：dump 页面结构 —— 高频 class、含关键词的容器、按钮
_PROBE_JS = """
() => {
  const all = document.querySelectorAll('*');
  const freq = {};
  for (const el of all) {
    const cls = typeof el.className === 'string' ? el.className : '';
    for (const c of cls.split(/\\s+/)) {
      if (!c) continue;
      freq[c] = (freq[c] || 0) + 1;
    }
  }
  const classes = Object.entries(freq).sort((a, b) => b[1] - a[1]).slice(0, 120)
    .map(([c, n]) => c + '(' + n + ')');
  const kw = ['markdown', 'think', 'message', 'answer', 'assistant', 'response',
              'bubble', 'chat-item', 'conversation', 'send', 'stop', 'generate'];
  const hits = [];
  for (const el of all) {
    const cls = typeof el.className === 'string' ? el.className : '';
    if (kw.some((k) => cls.toLowerCase().includes(k))) {
      hits.push({
        tag: el.tagName,
        cls: cls.slice(0, 150),
        text: (el.textContent || '').trim().slice(0, 150),
      });
      if (hits.length > 60) break;
    }
  }
  return { classes: classes, hits: hits, bodyTextLen: document.body.innerText.length };
}
"""

CHAT_ID_PREFIX = "chatcmpl-"


def _chat_id() -> str:
    return CHAT_ID_PREFIX + uuid.uuid4().hex[:16]


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _stream_fc(
    chat_id: str,
    model: str,
    thread_id: str | None,
    content: str | None,
    thinking: str | None,
    tool_calls: list[dict] | None,
    finish: str,
    widgets: list[dict] | None = None,
):
    """Function Calling 的流式输出：先缓冲后发（工具场景无法边流边发）。

    - 有 tool_calls → 发两段式 tool_call delta（id/name → arguments），finish=tool_calls
    - 否则 → 一次性发 content，finish=stop
    """
    meta = {"id": chat_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
    if thread_id:
        meta["thread_id"] = thread_id
    yield _sse({**meta, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    if thinking:
        yield _sse({**meta, "choices": [{"index": 0, "delta": {"reasoning_content": thinking}, "finish_reason": None}]})
    if tool_calls:
        for i, tc in enumerate(tool_calls):
            fn = tc.get("function", {})
            yield _sse(
                {**meta, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, "id": tc.get("id"), "type": "function", "function": {"name": fn.get("name"), "arguments": ""}}]}, "finish_reason": None}]}
            )
            yield _sse(
                {**meta, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": i, "function": {"arguments": fn.get("arguments", "")}}]}, "finish_reason": None}]}
            )
    elif content:
        yield _sse({**meta, "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}]})
    yield _sse({**meta, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    yield "data: [DONE]\n\n"


def _history_attachments(atts: list[dict] | None) -> list[dict] | None:
    """附件→历史存储形态（data 过大时只留元信息，避免 DB 膨胀）。"""
    if not atts:
        return None
    out = []
    for a in atts:
        item = {"type": a.get("type", "image"), "mime": a.get("mime"), "name": a.get("name")}
        if a.get("url"):
            item["url"] = a["url"]
        elif a.get("data") and len(a["data"]) <= 2_000_000:
            item["data"] = a["data"]
        out.append(item)
    return out or None


def create_router(registry: ProviderRegistry, threads: ThreadManager | None = None) -> APIRouter:
    router = APIRouter()
    thread_parallel = threads is not None and registry.config.server.thread_parallel

    # ---------------- OpenAI 兼容 API ----------------

    @router.get("/v1/models")
    async def list_models():
        return {"object": "list", "data": registry.list_models()}

    @router.post("/v1/chat/completions")
    async def chat_completions(req: ChatCompletionRequest, request: Request):
        provider = registry.get_for_model(req.model)
        if req.n and req.n > 1:
            raise RateLimitedError("n>1 不受支持，请使用 n=1", provider=provider.name)

        messages, attachments = [], []
        for m in req.messages:
            nm, atts = normalize_message(m.model_dump())
            messages.append(nm)
            attachments.extend(atts)
        model = req.model                       # 响应回显请求名（OpenAI 兼容）
        resolved = registry.resolve_model_name(model)  # 驱动用真实模型名
        chat_id = _chat_id()

        # 闸门排队时长记进时间线（区分"排队慢"与"生成慢"）；闸门本身不区分优先级。
        on_queued = getattr(provider, "note_queued", None)

        thread_id = req.thread_id or request.headers.get("x-thread-id")
        if not thread_id and threads is not None:
            # 无 thread_id 请求也保存为可回访会话（playground「新会话」左侧列表需要）：
            # 自动分配 thread_id，标题=第一句 user 消息，之后可点击切换回来续用。
            thread_id = f"auto-{uuid.uuid4().hex[:8]}"
        if thread_id:
            if threads is None:
                raise ProviderError("会话绑定未启用（threads manager 未初始化）")
            tm: ThreadManager = threads
            # 第一句 user 消息作会话标题（create 时记录；resume 忽略，保留原标题）
            first_msg = next(
                (m["content"] for m in messages if m.get("role") == "user" and m.get("content")),
                "",
            )
            session, mode = await tm.get_or_create(
                thread_id, provider, resolved, first_message=first_msg[:60]
            )
            kwargs = {"thread_mode": mode, "thread_page": session.page}
            # 实际发给页面的新用户消息（resume 只发最后一条）→ 历史入库用这条
            user_text = last_user_message(messages)
            if req.mode is not None:
                kwargs["mode"] = req.mode
            if req.deep_think is not None:
                kwargs["deep_think"] = req.deep_think
            if req.search is not None:
                kwargs["search"] = req.search
            if attachments:
                kwargs["attachments"] = attachments
            if req.options:
                kwargs["options"] = req.options
            # Function Calling：协议层拼 prompt；流式带工具走“先缓冲后发”
            fc_active = bool(
                registry.config.server.function_calling
                and needs_tool_handling(messages, req.tools)
            )
            if fc_active:
                kwargs["prompt_override"] = (
                    fc_build_resume_prompt(messages, req.tools, req.tool_choice)
                    if mode == "resume"
                    else fc_build_prompt(messages, req.tools, req.tool_choice)
                )

            if req.stream and fc_active:
                try:
                    async with session.lock:                     # ① 会话锁（页面只有一个输入框）
                        content, thinking = await asyncio.wait_for(
                            provider.gate.run(                        # ② 再拿闸门（锁序固定，防死锁）
                                provider.complete, messages, resolved,
                                on_queued=on_queued, **kwargs,
                            ),
                            timeout=provider.cfg.response_timeout + 60,
                        )
                    await tm.persist(thread_id)
                    await tm.save_turn(
                        thread_id, provider.name, resolved, user_text, content, thinking,
                        _history_attachments(attachments), provider.take_widgets(),
                    )
                except asyncio.TimeoutError:
                    await tm.close(thread_id)
                    raise ThreadTimeoutError(
                        f'thread "{thread_id}" 响应超时，会话已销毁，请重试'
                    )
                except ThreadExpiredError:
                    await tm.close(thread_id)
                    raise
                except ThreadBusyError:
                    await tm.close(thread_id)
                    raise
                c, calls, finish = (
                    parse_tool_response(content, req.tools) if req.tools else (content, None, "stop")
                )
                return StreamingResponse(
                    _stream_fc(
                        chat_id, model, thread_id, c, thinking, calls, finish,
                        provider.take_widgets(),
                    ),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )

            if req.stream:
                sink: dict = {}

                async def _gen():
                    # 同一 thread 串行（session.lock）：页面只有一个输入框
                    try:
                        await asyncio.wait_for(session.lock.acquire(), timeout=60)
                    except asyncio.TimeoutError:
                        await tm.close(thread_id)
                        raise ThreadTimeoutError(
                            f'thread "{thread_id}" 上一请求未释放（可能客户端中断），会话已销毁，请重试'
                        )
                    try:
                        content_parts: list[str] = []
                        thinking_parts: list[str] = []
                        # 会话锁 → 闸门（锁序固定）：thread 模式同样受 provider 级节奏/优先级约束
                        async for chunk in provider.gate.run_iter(
                            provider.generate, messages, resolved,
                            on_queued=on_queued, **kwargs,
                        ):
                            if chunk.kind == "thinking":
                                thinking_parts.append(chunk.text)
                            else:
                                content_parts.append(chunk.text)
                            yield chunk
                        await tm.persist(thread_id)  # 正常完成 → 落盘会话 URL id（重启可恢复）
                        # 历史入库（best-effort，内部已捕获异常，不影响流式响应）
                        widgets = provider.take_widgets()
                        if widgets and sink is not None:
                            sink["widgets"] = widgets
                        await tm.save_turn(
                            thread_id,
                            provider.name,
                            resolved,
                            user_text,
                            "".join(content_parts),
                            "".join(thinking_parts) or None,
                            _history_attachments(attachments),
                            widgets,
                        )
                    finally:
                        session.lock.release()

                return StreamingResponse(
                    _stream_thread_completions(_gen(), chat_id, model, thread_id, tm, sink),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )

            try:
                async with session.lock:
                    content, thinking = await asyncio.wait_for(
                        provider.gate.run(
                            provider.complete, messages, resolved,
                            on_queued=on_queued, **kwargs,
                        ),
                        timeout=provider.cfg.response_timeout + 60,
                    )
                await tm.persist(thread_id)  # 正常完成 → 落盘会话 URL id（重启可恢复）
                # 历史入库（best-effort）
                await tm.save_turn(
                    thread_id, provider.name, resolved, user_text, content, thinking,
                    _history_attachments(attachments), provider.take_widgets(),
                )
            except asyncio.TimeoutError:
                await tm.close(thread_id)  # 销毁：页面关闭强制打断挂起的 Playwright 调用
                raise ThreadTimeoutError(
                    f'thread "{thread_id}" 响应超时，会话已销毁，请重试'
                )
            except ThreadExpiredError:
                await tm.close(thread_id)  # 页面失效 → 销毁，客户端可重建
                raise
            except ThreadBusyError:
                await tm.close(thread_id)  # 页面忙（上一请求未完成）→ 销毁，客户端可重建
                raise
            widgets = provider.take_widgets()
            message = ResponseMessage(content=content, reasoning_content=thinking, widgets=widgets or None)
            finish = "stop"
            if fc_active and req.tools:
                content, calls, finish = parse_tool_response(content, req.tools)
                message = ResponseMessage(
                    content=content, reasoning_content=thinking, tool_calls=calls,
                    widgets=widgets or None,
                )
            return ChatCompletionResponse(
                id=chat_id,
                created=int(time.time()),
                model=model,
                thread_id=thread_id,
                choices=[ChatCompletionChoice(message=message, finish_reason=finish)],
            )

        opts = {}
        if req.mode is not None:
            opts["mode"] = req.mode
        if req.deep_think is not None:
            opts["deep_think"] = req.deep_think
        if req.search is not None:
            opts["search"] = req.search
        if attachments:
            opts["attachments"] = attachments
        if req.options:
            opts["options"] = req.options

        # Function Calling（无状态）：协议层拼 prompt；流式带工具先缓冲后发
        fc_active = bool(
            registry.config.server.function_calling and needs_tool_handling(messages, req.tools)
        )
        if fc_active:
            opts["prompt_override"] = fc_build_prompt(messages, req.tools, req.tool_choice)

        if req.stream and fc_active:
            content, thinking = await provider.gate.run(
                provider.complete, messages, resolved, on_queued=on_queued, **opts
            )
            c, calls, finish = (
                parse_tool_response(content, req.tools) if req.tools else (content, None, "stop")
            )
            return StreamingResponse(
                _stream_fc(chat_id, model, None, c, thinking, calls, finish),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        if req.stream:
            return StreamingResponse(
                _stream_completions(
                    provider, messages, resolved, chat_id,
                    on_queued=on_queued, **opts,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        content, thinking = await provider.gate.run(
            provider.complete, messages, resolved, on_queued=on_queued, **opts
        )
        message = ResponseMessage(content=content, reasoning_content=thinking)
        finish = "stop"
        if fc_active and req.tools:
            content, calls, finish = parse_tool_response(content, req.tools)
            message = ResponseMessage(content=content, reasoning_content=thinking, tool_calls=calls)
        return ChatCompletionResponse(
            id=chat_id,
            created=int(time.time()),
            model=model,
            choices=[ChatCompletionChoice(message=message, finish_reason=finish)],
        )

    async def _stream_completions(provider, messages, model, chat_id, on_queued=None, **opts):
        created = int(time.time())
        # 首个 chunk：角色声明
        yield _sse(
            {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
        )
        try:
            async for chunk in provider.gate.run_iter(
                provider.generate, messages, model,
                on_queued=on_queued, **opts,
            ):
                delta = (
                    {"reasoning_content": chunk.text}
                    if chunk.kind == "thinking"
                    else {"content": chunk.text}
                )
                yield _sse(
                    {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                )
        except ProviderError as e:
            # 流中途失败：记录并结束流（客户端看到截断，日志里有原因）
            logger.error("stream error for %s: %s", model, e.message)
            return
        widgets = provider.take_widgets()
        tail = {"widgets": widgets} if widgets else {}
        yield _sse(
            {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                **tail,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        yield "data: [DONE]\n\n"

    async def _stream_thread_completions(
        agen, chat_id, model, thread_id, threads: ThreadManager | None, sink: dict | None = None
    ):
        """会话绑定模式的 SSE：与 _stream_completions 相同，另回显 thread_id，
        流中途页面失效（ThreadExpiredError）时销毁会话并结束流。"""
        created = int(time.time())
        meta = {"id": chat_id, "object": "chat.completion.chunk", "created": created,
                "model": model, "thread_id": thread_id}
        yield _sse({**meta, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
        try:
            async for chunk in agen:
                delta = (
                    {"reasoning_content": chunk.text}
                    if chunk.kind == "thinking"
                    else {"content": chunk.text}
                )
                yield _sse({**meta, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        except (ThreadExpiredError, ThreadBusyError) as e:
            if threads is not None:
                await threads.close(thread_id)
            logger.error("thread %s expired/busy mid-stream, closed", thread_id)
            # 错误透传：不静默空流，客户端能看到失败原因
            yield _sse({**meta, "choices": [{"index": 0, "delta": {"content": f"\n\n[会话错误] {e.message}"}, "finish_reason": None}]})
            yield "data: [DONE]\n\n"
            return
        except ProviderError as e:
            logger.error("thread stream error for %s: %s", model, e.message)
            yield _sse({**meta, "choices": [{"index": 0, "delta": {"content": f"\n\n[错误] {e.message}"}, "finish_reason": None}]})
            yield "data: [DONE]\n\n"
            return
        widgets = (sink or {}).get("widgets") or []
        tail = {"widgets": widgets} if widgets else {}
        yield _sse({**meta, **tail, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        yield "data: [DONE]\n\n"

    # ---------------- admin：系统状态 ----------------

    @router.get("/admin/repo")
    async def repo_endpoint():
        """仓库信息（UI 顶部 Star 入口 + 星标数）。

        后端带缓存拉取（GitHub 匿名限流 60/h），失败返回 ``error`` 且不影响界面；
        未配置 ``server.repo_url`` 时全部为 null（UI 自动隐藏）。
        """
        return await asyncio.to_thread(repo_info, registry.config.server.repo_url)

    # ---------------- 画面交互（control）：把点/拖/打字变成真实输入 ----------------

    _input_locks: dict[str, asyncio.Lock] = {}

    @router.post("/admin/{name}/input")
    async def live_input(name: str, body: LiveInputRequest, thread_id: str | None = None):
        """画面交互（点击/拖拽/输入）。

        **默认关闭**：``server.live_control=false`` 时一律 403（UI 会自动隐藏交互区）。
        设计见 docs/LIVE_CONTROL.md（真实输入事件、拖拽一次请求内完成、同 provider 串行）。
        """
        if not registry.config.server.live_control:
            return JSONResponse(
                status_code=403,
                content={"error": {"message": "画面交互未开启：请在 config.yaml 设 server.live_control: true"}},
            )
        req = _InputAction(
            action=body.action, x=body.x, y=body.y, x2=body.x2, y2=body.y2, dx=body.dx, dy=body.dy,
            text=body.text, key=body.key, steps=body.steps, delay_ms=body.delay_ms, button=body.button,
        )
        err = req.validate()                 # 先校验参数（与目标无关的坏输入一律 400）
        if err:
            return JSONResponse(status_code=400, content={"error": {"message": err}})
        if name not in registry.providers():
            return JSONResponse(status_code=404, content={"error": {"message": f"未知 provider：{name}"}})

        page, shown_thread = await _live_page(name, thread_id)
        if page is None:
            return _live_no_page(name)
        # 若锁定了某个会话且它正在生成 → 画面交互属于"体验"，直接 409 让路（见 docs/CONCURRENCY.md）
        session_lock = None
        if thread_id and threads is not None:
            sess = threads.get(thread_id)
            session_lock = getattr(sess, "lock", None)
        if session_lock is not None and session_lock.locked():
            return JSONResponse(
                status_code=409,
                content={"error": {"message": "该会话正在生成，画面交互请稍后再试（API 优先）"}},
            )
        # reopen 目标：会话页 → 回该会话 URL（重开新文档、不丢上下文）；否则回 provider 首页。
        # 只允许这两个同源目标（不接受任意 URL）。
        provider = registry.providers()[name]
        reopen_url = provider.cfg.url
        if shown_thread and threads is not None:
            sess = threads.get(shown_thread)
            url_id = getattr(sess, "url_id", None)
            restore_url = provider.session_url(url_id) if url_id else None
            if restore_url:
                reopen_url = restore_url
        lock = _input_locks.setdefault(name, asyncio.Lock())
        async with lock:                     # 同一 provider 串行，避免动作交错成坏序列
            try:
                result = await _apply_input(page, req, reopen_url=reopen_url)
            except Exception as exc:  # noqa: BLE001  页面在动/元素失效等
                logger.warning("live input failed provider=%s action=%s: %s", name, req.action, exc)
                return JSONResponse(
                    status_code=503, content={"error": {"message": f"输入失败：{exc}"}}
                )
        logger.info(
            "live input provider=%s action=%s mapped=%s elapsed=%sms",
            name, req.action, result.get("mapped"), result.get("elapsed_ms"),
        )
        return {"ok": True, **result}

    @router.get("/admin/timeline")
    async def timeline_endpoint(provider: str | None = None, limit: int = 50):
        """最近若干次请求的时间线（"慢在哪"的诊断窗口；后续可改为入库）。

        每条包含：setup/send/first_think/first_content/settled/done/final 各时间点，
        以及 ttft（首字延迟）、settle_lag（我们比站点慢多少）、tail（收尾开销）、
        polls / extract_ms_total / deltas / chars 等计数。
        """
        limit = max(1, min(int(limit), 200))
        items = TIMELINES.recent(provider=provider, limit=limit)
        return {"count": len(items), "provider": provider, "items": items}

    @router.get("/admin/status")
    async def system_status(request: Request):
        """聚合状态：服务信息 + 各 provider 认证状态 + 活跃 thread 会话。"""
        cfg = registry.config
        started_at = getattr(request.app.state, "started_at", time.monotonic())
        providers = []
        for name, p in registry.providers().items():
            pcfg = p.cfg
            state_file = Path(cfg.profiles_dir) / name / "state.json"
            logged_in = registry.login_status().get(name, False)
            # 认证有效期：只认配置的 auth_cookies；未配置/无法判断 → unknown
            auth = compute_for_state_file(
                state_file,
                auth_cookies=pcfg.login.auth_cookies,
                session_ttl_days=pcfg.login.session_ttl_days,
                auth_local_storage=pcfg.login.auth_local_storage,
                warn_days=pcfg.login.expiry_warn_days or cfg.browser.auth_expiry_warn_days,
                login_at=p.browser.read_login_at(name),
            ).to_dict()
            if not logged_in:
                auth["state"] = "logged_out"
            providers.append(
                {
                    "name": name,
                    "driver": pcfg.driver or name,
                    "enabled": pcfg.enabled,
                    "url": pcfg.url,
                    "logged_in": logged_in,
                    "login_mode": pcfg.login.mode,
                    "auth_expiry": auth,
                    "has_state_file": state_file.exists(),
                    "login_error_screenshot": p.browser.login_error_path(name).exists(),
                    "login_error_at": p.browser.login_error_mtime(name),
                    "queue": (lambda g: g.stats() if g is not None else None)(
                        getattr(p, "gate", None)
                    ),
                    "models": [m.name for m in pcfg.models],
                    "model_aliases": pcfg.model_aliases,
                    "default_model": p.exposed_models[0] if p.exposed_models else None,
                    "response_timeout": pcfg.response_timeout,
                }
            )
        thread_list = threads.list() if threads is not None else []
        return {
            "server": {
                "version": "0.1.0",
                # UI 据此决定是否显示"画面交互"入口（避免点了没反应=按钮状态错误）
                "live_view": cfg.server.live_view,
                "live_control": cfg.server.live_control,
                "uptime_seconds": round(time.monotonic() - started_at, 1),
                "host": cfg.server.host,
                "port": cfg.server.port,
                "headless": cfg.browser.headless,
                "locale": cfg.browser.locale,
                "status_check": cfg.browser.status_check,
                "status_check_headless": cfg.browser.status_check_headless,
                "thread_ttl": cfg.server.thread_ttl,
                "max_threads": cfg.server.max_threads,
                "thread_parallel": cfg.server.thread_parallel,
                "thread_persist": cfg.server.thread_persist,
                "api_keys_configured": bool(cfg.server.api_keys),
            },
            "providers": providers,
            "threads": {
                "active": threads.active_count() if threads is not None else 0,
                "max": threads.max_threads if threads is not None else 0,
                "total": len(thread_list),
                "list": thread_list,   # 供 provider 摘要统计（会话浏览请用 /ui/threads.html）
            },
        }

    # ---------------- admin：实时画面 Live View（只读，见 docs/LIVE_VIEW.md） ----------------

    async def _live_page(name: str, focus: str | None = None):
        """选页（**不创建**），返回 ``(page, thread_id)``。

        顺序：① ``focus`` 指定的会话 → ② 该 provider **最近使用**的会话页面
        → ③ 已有 context 的最后一个页面 → ④ None。

        ⚠ ②必须按"最近使用"排序：字典顺序是"最早创建"，会让画面停在你没在用的那个会话上。
        """
        candidates = threads.live_pages(name) if threads is not None else []
        if focus:
            candidates.sort(key=lambda item: item[0] != focus)   # 命中的排最前
        for thread_id, page in candidates:
            try:
                if not page.is_closed():
                    return page, thread_id
            except Exception:  # noqa: BLE001  页面刚关闭会抛
                continue
        ctx = registry.browser.active_context(name)
        if ctx is not None:
            for page in reversed(ctx.pages):
                try:
                    if not page.is_closed():
                        return page, None
                except Exception:  # noqa: BLE001
                    continue
        return None, None

    def _live_busy(name: str) -> bool:
        """有请求在跑**或有人在排队** → 实时画面降帧（CPU 让给生成；UI 只做体验）。"""
        provider = registry.providers().get(name)
        gate = getattr(provider, "gate", None) if provider is not None else None
        if gate is None:
            return False
        return bool(gate.busy or gate.has_waiters)

    async def _live_resolve(name: str, focus: str | None = None):
        """给 LiveController 用：只要 page（thread_id 由 _live_page 单独提供）。"""
        page, _tid = await _live_page(name, focus)
        return page

    live = LiveController(
        _live_resolve, busy_checker=_live_busy, config=getattr(registry, "config", None)
    )

    def _live_disabled():
        return JSONResponse(
            status_code=403,
            content={"error": {"message": "实时画面已禁用（config.yaml: server.live_view=false）"}},
        )

    def _live_unknown(name: str):
        return JSONResponse(
            status_code=404, content={"error": {"message": f"provider {name!r} 不存在或未启用"}}
        )

    def _live_no_page(name: str):
        return JSONResponse(
            status_code=503,
            content={"error": {"message": f"没有可截图的页面（{name} 当前未打开任何页面）"}},
        )

    _widgets_store_holder: dict = {}

    def _widgets_store() -> WidgetStore:
        """懒创建：只有真正访问组件文件接口时才依赖 ``registry.config``。"""
        store = _widgets_store_holder.get("store")
        if store is None:
            store = WidgetStore(registry.config.profiles_dir)
            _widgets_store_holder["store"] = store
        return store

    @router.get("/admin/{name}/widgets/{widget_id}.png")
    async def widget_png(name: str, widget_id: str):
        """交互组件截图（保真兜底）。"""
        path = _widgets_store().path(name, widget_id, "png")
        if path is None:
            return JSONResponse(status_code=404, content={"error": {"message": "组件不存在"}})
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    @router.get("/admin/{name}/widgets/{widget_id}.html")
    async def widget_html(name: str, widget_id: str):
        """交互组件 HTML（含内联脚本）。

        ⚠ 直接打开会在**同源**下执行组件脚本 → 用 CSP `sandbox` 强制降级为不透明源
        （只允许脚本，不允许访问宿主页面/DOM），Playground 侧另有 iframe sandbox。
        """
        path = _widgets_store().path(name, widget_id, "html")
        if path is None:
            return JSONResponse(status_code=404, content={"error": {"message": "组件不存在"}})
        return Response(
            content=path.read_bytes(),
            media_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "sandbox allow-scripts",
            },
        )

    @router.post("/admin/{name}/dismiss")
    async def dismiss_overlays(name: str, thread_id: str | None = None):
        """关闭该 provider 页面上的弹窗/遮罩（配置的 ``selectors.dismiss_button``）。

        用途：弹窗挡住输入导致发送失败时，先手工清障（UI 画面页也有对应按钮）。
        """
        provider = registry.providers().get(name)
        if provider is None:
            return _live_unknown(name)
        page, shown = await _live_page(name, thread_id)
        if page is None:
            return _live_no_page(name)
        try:
            dismissed = await provider._dismiss_overlays(page, reason="手工清障")
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=503, content={"error": {"message": f"关闭弹窗失败：{exc}"}}
            )
        return {"provider": name, "shown_thread_id": shown, "dismissed": dismissed}

    def _frame_opts(name: str, *, with_fps: bool = False, **kw):
        """按 provider 配置组装截帧参数（``crop=last`` 需要知道正文容器选择器）。"""
        cfg = registry.config
        p = registry.providers().get(name)
        sels = list(getattr(getattr(p, "cfg", None), "selectors", None).response_container or []) if p else []
        kw.setdefault("crop_selector", sels[0] if sels else None)
        kw.setdefault("default_crop", cfg.server.live_crop)
        if with_fps:
            kw.setdefault("default_fps", cfg.server.live_fps)
        return parse_frame_options(default_quality=cfg.server.live_quality, **kw)

    @router.get("/admin/{name}/screen.jpg")
    async def screen_jpg(
        name: str,
        quality: int | None = None,
        clip: str | None = None,
        crop: str | None = None,
        thread_id: str | None = None,
    ):
        """单帧 JPEG（只读）。``quality`` 1-95、``clip=x,y,w,h``、``crop=last``（只裁最后一条回复）。"""
        if not registry.config.server.live_view:
            return _live_disabled()
        if name not in registry.providers():
            return _live_unknown(name)
        opts = _frame_opts(name, quality=quality, clip=clip, crop=crop)
        try:
            frame = await live.frame_once(name, opts, thread_id)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=503, content={"error": {"message": f"截图失败：{exc}"}}
            )
        if frame is None:
            return _live_no_page(name)
        return Response(
            content=frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )

    @router.get("/admin/{name}/stream.mjpg")
    async def stream_mjpg(
        name: str,
        request: Request,
        fps: float | None = None,
        quality: int | None = None,
        clip: str | None = None,
        crop: str | None = None,
        thread_id: str | None = None,
    ):
        """MJPEG 直播（只读）：浏览器 `<img src>` 原生渲染。``crop=last`` 只裁最后一条回复。

        多观众共享同一采集循环（扇出）；页面不存在时不主动创建 → 直接 503。
        """
        if not registry.config.server.live_view:
            return _live_disabled()
        if name not in registry.providers():
            return _live_unknown(name)
        if (await _live_page(name, thread_id))[0] is None:
            return _live_no_page(name)
        opts = _frame_opts(name, with_fps=True, fps=fps, quality=quality, clip=clip, crop=crop)

        async def gen():
            async for frame in live.subscribe(name, opts, thread_id):
                if await request.is_disconnected():
                    break
                yield mjpeg_part(frame)

        return StreamingResponse(
            gen(),
            media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.get("/admin/{name}/screen/state")
    async def screen_state(name: str, thread_id: str | None = None):
        """直播状态：可用性 / 观众数 / 参数 / 页面信息 / 是否正在生成 / 当前画面来自哪个会话。"""
        if not registry.config.server.live_view:
            return _live_disabled()
        if name not in registry.providers():
            return _live_unknown(name)
        page, shown_thread = await _live_page(name, thread_id)
        page_url = None
        viewport = None
        if page is not None:
            try:
                page_url = page.url
                size = page.viewport_size
                viewport = dict(size) if size else None
            except Exception:  # noqa: BLE001
                pass
        return {
            "provider": name,
            "available": page is not None,
            "shown_thread_id": shown_thread,     # 画面来自哪个会话（None = 非会话页，如登录页）
            "page_url": page_url,
            "viewport": viewport,
            "busy": _live_busy(name),
            **live.state(name, thread_id),
        }

    # ---------------- admin：会话绑定管理 ----------------

    @router.get("/admin/threads")
    async def threads_list(
        q: str | None = None,
        provider: str | None = None,
        limit: int = 0,
        offset: int = 0,
        order: str = "desc",
    ):
        """会话列表（过滤/分页/排序）。``limit=0`` = 不限制（兼容旧调用方）。"""
        if threads is None:
            return {
                "threads": [], "total": 0, "limit": limit, "offset": offset,
                "has_more": False, "active": 0, "max": 0,
            }
        return threads.page(
            q=q,
            provider=provider,
            limit=max(0, min(limit, 500)),
            offset=max(0, offset),
            order="asc" if order == "asc" else "desc",
        )

    @router.delete("/admin/threads/{thread_id}")
    async def threads_delete(thread_id: str):
        if threads is None:
            return {"thread_id": thread_id, "closed": False}
        closed = await threads.close(thread_id)
        return {"thread_id": thread_id, "closed": closed}

    @router.get("/admin/threads/{thread_id}/messages")
    async def threads_messages(thread_id: str):
        """某会话的历史消息（Playground 切换会话时回填）。"""
        if threads is None:
            return {"thread_id": thread_id, "messages": []}
        return {"thread_id": thread_id, "messages": await threads.get_messages(thread_id)}

    @router.get("/admin/threads/{thread_id}/dom")
    async def threads_dom(thread_id: str, selector: str = "div.ds-message"):
        """调试：直接查询指定 thread 页面的 DOM（绑定页面的真实状态）。"""
        if threads is None:
            return {"thread_id": thread_id, "error": "threads manager 未初始化"}
        session = threads.get(thread_id)
        if session is None:
            return {"thread_id": thread_id, "error": "thread not found"}
        try:
            loc = session.page.locator(selector)
            count = await loc.count()
            snippets = []
            for i in range(min(count, 5)):
                try:
                    html = await loc.nth(i).evaluate("el => el.outerHTML")
                    snippets.append(html[:500])
                except Exception:
                    snippets.append("<error>")
            return {"thread_id": thread_id, "url": session.page.url, "count": count, "snippets": snippets}
        except Exception as e:
            return {"thread_id": thread_id, "error": str(e)}

    # ---------------- admin：登录管理 ----------------

    @router.post("/admin/{name}/login/start")
    async def login_start(name: str):
        provider = registry.get_provider(name)
        page = await provider.browser.open_page(name, locale=provider.locale)
        await page.goto(provider.login_url, wait_until="domcontentloaded", timeout=30000)
        headless = registry.config.browser.headless
        hint = provider.cfg.login.hint or f"请在浏览器窗口完成登录后调用 /admin/{name}/login/status"
        if headless:
            # headless 下窗口不可见，手动登录会卡住 → 明确给出恢复办法（不要静默失败）
            hint = (
                "当前 browser.headless=true（窗口不可见，无法手动登录）。"
                "手动登录请设 DEEPSEEK_HEADLESS=false 后重启服务，再调用本接口；"
                f"或直接用已配置的自动登录：POST /admin/{name}/login/auto"
            )
        return {
            "status": "started",
            "provider": name,
            "headless": headless,
            "hint": hint,
        }

    @router.get("/admin/{name}/login/status")
    async def login_status(name: str):
        provider = registry.get_provider(name)
        was = registry.login_status().get(name, False)
        ok = await provider.check_login()
        if ok is None:
            # 页面未就绪/网络异常 → 不确定，保持上次状态（不要误报未登录）
            return {"provider": name, "logged_in": was, "inconclusive": True}
        if ok and not was:
            # 刚从不登录变登录（多为手动登录完成）→ 登录态已变，必须落盘
            provider.browser.mark_state_dirty(name)
        if ok:
            await provider.browser.save_state(name)  # 已登录且未过期时不写
        registry.set_login_status(name, ok)
        return {"provider": name, "logged_in": ok}

    @router.post("/admin/{name}/login/auto")
    async def login_auto(name: str):
        """用 .env 中的账号密码自动登录（login.mode=auto 时可用）。"""
        provider = registry.get_provider(name)
        result = await provider.gate.run(provider.auto_login)
        ok = bool(result.get("ok"))
        registry.set_login_status(name, ok)
        return {"provider": name, **result}

    @router.post("/admin/{name}/login/state")
    async def login_state(name: str, payload: StorageStatePayload):
        """导入完整 storage_state（cookies+localStorage）：写盘 + 重置 context → **立即生效**。

        供手动登录：宿主用 `python -m ai_web2api.cli login <provider>` 生成 state 后自动/手动导入。
        """
        provider = registry.get_provider(name)
        if not payload.cookies:
            return JSONResponse(
                status_code=400, content={"error": "storage_state.cookies 为空"}
            )
        path = provider.browser.state_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {"cookies": payload.cookies, "origins": payload.origins}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        # 旧 context/页面随之失效：先关该 provider 的活跃 thread 会话（保留历史）
        if threads is not None:
            try:
                await threads.close_provider(name)
            except Exception:  # noqa: BLE001
                logger.warning("close_provider(%s) 失败", name, exc_info=True)
        await provider.browser.reset_context(name)
        provider.browser.clear_login_error(name)
        provider.browser.record_login_at(name)  # 导入即一次新登录（不随轮换变化）
        ok = await provider.check_login()
        if ok is not None:
            registry.set_login_status(name, ok)
        return {
            "provider": name,
            "logged_in": registry.login_status().get(name, False),
            "imported": True,
        }

    @router.post("/admin/{name}/login/cookies")
    async def login_cookies(name: str, payload: CookiesPayload):
        """只导入 cookies（兼容旧接口）；需要 localStorage 时用 login/state。"""
        provider = registry.get_provider(name)
        ctx = await provider.browser.get_context(name, locale=provider.locale)
        # playwright 的 dict 形式使用 camelCase 字段名，与 CookieItem.model_dump() 一致
        await ctx.add_cookies([c.model_dump() for c in payload.cookies])  # type: ignore[arg-type]
        # 注入即变更：必须落盘（不受"已登录未过期就不写"规则约束）
        provider.browser.mark_state_dirty(name)
        await provider.browser.save_state(name)
        provider.browser.record_login_at(name)  # cookies 注入 = 一次新登录
        ok = await provider.check_login()
        if ok is not None:
            registry.set_login_status(name, ok)
        return {"provider": name, "logged_in": registry.login_status().get(name, False)}

    @router.post("/admin/{name}/login/logout")
    async def login_logout(name: str):
        provider = registry.get_provider(name)
        await provider.browser.clear_state(name)
        registry.set_login_status(name, False)
        return {"provider": name, "logged_in": False}

    @router.get("/admin/{name}/login/screenshot")
    async def login_screenshot(name: str):
        """上次登录失败截图（PNG）；没有则 404。供状态面板调试展示。"""
        provider = registry.get_provider(name)
        path = provider.browser.login_error_path(name)
        if not path.exists():
            return JSONResponse(status_code=404, content={"error": "no screenshot"})
        return FileResponse(
            str(path), media_type="image/png", headers={"Cache-Control": "no-store"}
        )

    @router.post("/admin/{name}/login/screenshot")
    async def login_screenshot_capture(name: str):
        """即时抓取当前登录页/聊天页截图（调试用，覆盖上次失败截图）。"""
        provider = registry.get_provider(name)
        page = await provider.browser.open_page(name, locale=provider.locale)
        try:
            await page.goto(provider.login_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
            saved = await provider.browser.save_login_error(name, page)
        finally:
            await page.close()
        return {"provider": name, "saved": bool(saved)}

    # ---------------- admin：调试 ----------------

    @router.get("/admin/{name}/debug/dom")
    async def debug_dom(name: str, selector: str, index: int = 0):
        """返回页面中匹配 selector 的元素 HTML，用于排查选择器失效。"""
        provider = registry.get_provider(name)
        page = await provider.browser.open_page(name, locale=provider.locale)
        try:
            await page.goto(provider.cfg.url, wait_until="domcontentloaded", timeout=30000)
            result = await page.evaluate(
                "(args) => { const nodes = document.querySelectorAll(args.s); "
                "const el = nodes[args.i]; "
                "return { count: nodes.length, html: el ? el.outerHTML.slice(0, 4000) : null, "
                "url: location.href, bodyText: document.body.innerText.slice(0, 200) }; }",
                {"s": selector, "i": index},
            )
        finally:
            await page.close()
        return {"selector": selector, "index": index, **result}

    @router.post("/admin/{name}/debug/probe")
    async def debug_probe(name: str, payload: ProbePayload):
        """发一条消息并 dump 响应区 DOM 结构（用于确定新 UI 的响应/思考容器选择器）。"""
        provider = registry.get_provider(name)
        page = await provider.browser.open_page(name, locale=provider.locale)
        try:
            await page.goto(provider.cfg.url, wait_until="domcontentloaded", timeout=30000)
            input_sel = await extractor.first_match(page, provider.cfg.selectors.input)
            if input_sel is None:
                return {"error": "input selector 未匹配", "url": page.url}
            await page.locator(input_sel).first.click()
            await page.locator(input_sel).first.fill(payload.text or "你好")
            await page.keyboard.press("Enter")
            # 等回复开始（最多 30s）
            try:
                await page.wait_for_function(
                    _PROBE_WAIT_JS,
                    arg={"text": payload.text or "你好", "inputSel": input_sel},
                    timeout=30000,
                )
            except Exception:
                pass
            await page.wait_for_timeout(payload.wait_ms)
            info = await page.evaluate(_PROBE_JS)
            return {
                "url": page.url,
                "input_selector": input_sel,
                "sent_text": payload.text or "你好",
                "probe": info,
            }
        finally:
            await page.close()

    return router
