"""Function Calling 请求/响应 schema（Step 1）。

运行：.venv/bin/python -m pytest tests/test_fc_schema.py -v
"""

from __future__ import annotations

from ai_web2api.api.schemas import (
    ChatCompletionRequest,
    ResponseMessage,
    normalize_message,
)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]


def test_request_parses_tools_and_tool_choice():
    req = ChatCompletionRequest.model_validate(
        {
            "model": "deepseek-web",
            "messages": [{"role": "user", "content": "东京天气"}],
            "tools": _TOOLS,
            "tool_choice": "auto",
        }
    )
    assert req.tools and req.tools[0].function.name == "get_weather"
    assert req.tools[0].function.parameters["type"] == "object"
    assert req.tool_choice == "auto"


def test_tool_choice_object_and_required():
    for tc in ("none", "required"):
        req = ChatCompletionRequest.model_validate(
            {"model": "m", "messages": [{"role": "user", "content": "x"}], "tool_choice": tc}
        )
        assert req.tool_choice == tc
    req = ChatCompletionRequest.model_validate(
        {
            "model": "m",
            "messages": [{"role": "user", "content": "x"}],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        }
    )
    assert req.tool_choice.function.name == "get_weather"  # type: ignore[union-attr]


def test_no_tools_still_parses():
    """未传 tools 时行为不变（向后兼容）。"""
    req = ChatCompletionRequest.model_validate(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert req.tools is None and req.tool_choice is None


def test_assistant_tool_calls_and_tool_message_preserved():
    msgs = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"temp": 20}'},
        {"role": "user", "content": "总结"},
    ]
    for m in msgs:
        _, atts = normalize_message(m)
        assert atts == []
    a, _ = normalize_message(msgs[0])
    assert a["content"] == "" and a["tool_calls"][0]["function"]["name"] == "get_weather"
    t, _ = normalize_message(msgs[1])
    assert t["role"] == "tool" and t["tool_call_id"] == "call_1"


def test_response_message_tool_calls_shape():
    msg = ResponseMessage(
        content=None,
        tool_calls=[
            {
                "id": "call_x",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
            }
        ],
    )
    assert msg.content is None
    assert msg.model_dump()["tool_calls"][0]["id"] == "call_x"
