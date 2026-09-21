"""Function Calling 路由集成（Step 4/5）：stub provider，无需浏览器。

覆盖：非流式/流式 tool_calls、未命中回退文本、server.function_calling=false 忽略 tools。

运行：.venv/bin/python -m pytest tests/test_fc_routes.py -v
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_web2api.api.routes import create_router
from ai_web2api.config import AppConfig

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "get weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]
TOOL_TEXT = '```tool_json\n{"tool":"get_weather","parameters":{"city":"Tokyo"}}\n```'
PLAIN = "你好，这是普通回答。"


class _Gate:
    async def run(self, fn, *a, **kw):
        return await fn(*a, **kw)


class _Provider:
    name = "fake"

    def __init__(self, text: str) -> None:
        self.text = text
        self.gate = _Gate()
        self.cfg = SimpleNamespace()

    async def complete(self, messages, model, **kw):
        return self.text, None


class _Registry:
    def __init__(self, text: str, function_calling: bool) -> None:
        self.config = AppConfig.model_validate(
            {
                "server": {"function_calling": function_calling},
                "providers": [{"name": "fake", "url": "https://x", "models": [{"name": "fake-web"}]}],
            }
        )
        self.provider = _Provider(text)
        self.provider.cfg = self.config.providers[0]

    def get_for_model(self, model):
        return self.provider

    def resolve_model_name(self, m):
        return m

    def providers(self):
        return {"fake": self.provider}

    def list_models(self):
        return []

    def login_status(self):
        return {}


def _client(text: str, function_calling: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(create_router(_Registry(text, function_calling), None))  # type: ignore[arg-type]
    return TestClient(app)


def _body(text: str, **extra) -> dict:
    return {"model": "fake-web", "messages": [{"role": "user", "content": "东京天气"}], **extra}


def test_nonstream_tool_call():
    d = _client(TOOL_TEXT).post("/v1/chat/completions", json=_body(TOOL_TEXT, tools=TOOLS)).json()
    ch = d["choices"][0]
    assert ch["finish_reason"] == "tool_calls"
    assert ch["message"]["content"] is None
    tc = ch["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "get_weather"
    assert tc["id"].startswith("call_")
    assert tc["function"]["arguments"] == '{"city": "Tokyo"}'


def test_nonstream_plain_text():
    d = _client(PLAIN).post("/v1/chat/completions", json=_body(PLAIN, tools=TOOLS)).json()
    ch = d["choices"][0]
    assert ch["finish_reason"] == "stop"
    assert ch["message"]["content"] == PLAIN
    assert ch["message"].get("tool_calls") is None


def test_stream_tool_call():
    r = _client(TOOL_TEXT).post(
        "/v1/chat/completions", json=_body(TOOL_TEXT, tools=TOOLS, stream=True)
    )
    body = r.text
    assert '"tool_calls"' in body
    assert '"finish_reason": "tool_calls"' in body
    assert "[DONE]" in body
    assert '"name": "get_weather"' in body


def test_stream_plain_text():
    r = _client(PLAIN).post(
        "/v1/chat/completions", json=_body(PLAIN, tools=TOOLS, stream=True)
    )
    body = r.text
    assert PLAIN in body
    assert '"finish_reason": "stop"' in body
    assert "[DONE]" in body


def test_function_calling_disabled_ignores_tools():
    d = _client(TOOL_TEXT, function_calling=False).post(
        "/v1/chat/completions", json=_body(TOOL_TEXT, tools=TOOLS)
    ).json()
    ch = d["choices"][0]
    assert ch["finish_reason"] == "stop"
    assert ch["message"]["content"] == TOOL_TEXT  # 不解析，原文返回
