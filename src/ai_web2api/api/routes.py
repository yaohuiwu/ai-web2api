"""HTTP 路由：OpenAI 兼容 API + admin 登录管理。"""

from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import (
    JSONResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from ..browser import extractor
from ..core.errors import ProviderError, RateLimitedError
from ..providers.registry import ProviderRegistry
from .schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ResponseMessage,
    normalize_message,
)

logger = logging.getLogger(__name__)


class CookieItem(BaseModel):
    name: str
    value: str
    domain: str
    path: str = "/"
    expires: float | None = None
    httpOnly: bool = False
    secure: bool = False
    sameSite: str | None = None


class CookiesPayload(BaseModel):
    cookies: list[CookieItem]


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


def create_router(registry: ProviderRegistry) -> APIRouter:
    router = APIRouter()

    # ---------------- OpenAI 兼容 API ----------------

    @router.get("/v1/models")
    async def list_models():
        return {"object": "list", "data": registry.list_models()}

    @router.post("/v1/chat/completions")
    async def chat_completions(req: ChatCompletionRequest, request: Request):
        provider = registry.get_for_model(req.model)
        if req.n and req.n > 1:
            raise RateLimitedError("n>1 不受支持，请使用 n=1", provider=provider.name)

        messages = [normalize_message(m.model_dump()) for m in req.messages]
        model = req.model                       # 响应回显请求名（OpenAI 兼容）
        resolved = registry.resolve_model_name(model)  # 驱动用真实模型名
        chat_id = _chat_id()

        if req.stream:
            return StreamingResponse(
                _stream_completions(provider, messages, resolved, chat_id),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        content, thinking = await provider.gate.run(provider.complete, messages, resolved)
        message = ResponseMessage(content=content, reasoning_content=thinking)
        return ChatCompletionResponse(
            id=chat_id,
            created=int(time.time()),
            model=model,
            choices=[ChatCompletionChoice(message=message)],
        )

    async def _stream_completions(provider, messages, model, chat_id):
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
            async for chunk in provider.gate.run_iter(provider.generate, messages, model):
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
        yield _sse(
            {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        )
        yield "data: [DONE]\n\n"

    # ---------------- admin：登录管理 ----------------

    @router.post("/admin/{name}/login/start")
    async def login_start(name: str):
        provider = registry.get_provider(name)
        page = await provider.browser.open_page(name)
        await page.goto(provider.cfg.url, wait_until="domcontentloaded", timeout=30000)
        hint = provider.cfg.login.hint or f"请在浏览器窗口完成登录后调用 /admin/{name}/login/status"
        return {"status": "started", "provider": name, "hint": hint}

    @router.get("/admin/{name}/login/status")
    async def login_status(name: str):
        provider = registry.get_provider(name)
        ok = await provider.check_login()
        if ok:
            await provider.browser.save_state(name)
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

    @router.post("/admin/{name}/login/cookies")
    async def login_cookies(name: str, payload: CookiesPayload):
        provider = registry.get_provider(name)
        ctx = await provider.browser.get_context(name)
        # playwright 的 dict 形式使用 camelCase 字段名，与 CookieItem.model_dump() 一致
        await ctx.add_cookies([c.model_dump() for c in payload.cookies])  # type: ignore[arg-type]
        await provider.browser.save_state(name)
        ok = await provider.check_login()
        registry.set_login_status(name, ok)
        return {"provider": name, "logged_in": ok}

    @router.post("/admin/{name}/login/logout")
    async def login_logout(name: str):
        provider = registry.get_provider(name)
        await provider.browser.clear_state(name)
        registry.set_login_status(name, False)
        return {"provider": name, "logged_in": False}

    # ---------------- admin：调试 ----------------

    @router.get("/admin/{name}/debug/dom")
    async def debug_dom(name: str, selector: str, index: int = 0):
        """返回页面中匹配 selector 的元素 HTML，用于排查选择器失效。"""
        provider = registry.get_provider(name)
        page = await provider.browser.open_page(name)
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
        page = await provider.browser.open_page(name)
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
