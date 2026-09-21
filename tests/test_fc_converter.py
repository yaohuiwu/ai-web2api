"""Function Calling 转换层（Step 3）：tools/tool_choice → prompt，文本 → tool_calls。

运行：.venv/bin/python -m pytest tests/test_fc_converter.py -v
"""

from __future__ import annotations

from ai_web2api.api.schemas import ToolChoiceObject, ToolDefinition
from ai_web2api.tool_calling.converter import (
    build_prompt,
    build_resume_prompt,
    needs_tool_handling,
    normalize_tool_defs,
    parse_tool_response,
    resolve_effective_tools,
)

WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}
TIME = {"type": "function", "function": {"name": "get_time", "description": "Get time"}}


def test_resolve_effective_tools():
    assert resolve_effective_tools([WEATHER], None) == ([WEATHER], False)
    assert resolve_effective_tools([WEATHER], "auto") == ([WEATHER], False)
    assert resolve_effective_tools([WEATHER], "none") == ([], False)
    assert resolve_effective_tools([WEATHER], "required") == ([WEATHER], True)
    filtered, force = resolve_effective_tools([WEATHER, TIME], {"type": "function", "function": {"name": "get_time"}})
    assert filtered == [TIME] and force is True
    assert resolve_effective_tools([], "required") == ([], False)


def test_normalize_tool_defs_pydantic():
    td = ToolDefinition.model_validate(WEATHER)
    defs = normalize_tool_defs([td])
    assert defs[0]["name"] == "get_weather"
    assert defs[0]["parameters"]["type"] == "object"
    # 缺 parameters/description 也安全
    assert normalize_tool_defs([{"type": "function", "function": {"name": "f"}}])[0] == {
        "name": "f", "description": "", "parameters": {},
    }


def test_build_prompt_transcript():
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "东京天气？"},
    ]
    p = build_prompt(messages, [WEATHER], None)
    assert "get_weather" in p and "tool_json" in p  # 工具说明注入
    assert "System: 你是助手" in p and "Human: 东京天气？" in p


def test_build_prompt_assistant_tool_calls_and_result():
    messages = [
        {"role": "user", "content": "东京天气？"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"temp":20}'},
    ]
    p = build_prompt(messages, [WEATHER], None)
    assert "Assistant: [Called tools]" in p
    assert '{"tool":"get_weather","parameters":{"city":"Tokyo"}}' in p
    assert '<tool_result tool_call_id="call_1">' in p and '{"temp":20}' in p
    assert "根据以上工具" in p  # 末条是工具结果 → 续写提示


def test_build_resume_prompt_tool_result_only():
    messages = [
        {"role": "user", "content": "东京天气？"},
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "get_weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"temp":20}'},
    ]
    p = build_resume_prompt(messages, [WEATHER], None)
    assert '<tool_result tool_call_id="call_1">' in p
    assert "根据以上工具" in p
    assert "Human: 东京天气？" not in p  # resume 只发本轮，不回放历史


def test_build_resume_prompt_plain_user():
    p = build_resume_prompt([{"role": "user", "content": "继续"}], None, None)
    assert p == "继续"


def test_parse_tool_response_hit():
    text = '好的\n```tool_json\n{"tool":"get_weather","parameters":{"city":"Tokyo"}}\n```'
    content, calls, finish = parse_tool_response(text, [WEATHER])
    assert content is None and finish == "tool_calls"
    assert calls and calls[0]["id"].startswith("call_")
    assert calls[0]["function"]["name"] == "get_weather"
    assert calls[0]["function"]["arguments"] == '{"city": "Tokyo"}'


def test_parse_tool_response_unknown_name_falls_back_to_text():
    text = '```tool_json\n{"tool":"delete_everything","parameters":{}}\n```'
    content, calls, finish = parse_tool_response(text, [WEATHER])
    assert content == text and calls is None and finish == "stop"


def test_parse_tool_response_no_tools_or_no_call():
    assert parse_tool_response("普通回答", [WEATHER]) == ("普通回答", None, "stop")
    assert parse_tool_response("普通回答", None) == ("普通回答", None, "stop")


def test_needs_tool_handling():
    assert needs_tool_handling([{"role": "user", "content": "x"}], [WEATHER]) is True
    assert needs_tool_handling([{"role": "tool", "content": "r"}], None) is True
    assert needs_tool_handling([{"role": "user", "content": "x"}], None) is False


def test_tool_choice_object_pydantic():
    tc = ToolChoiceObject.model_validate({"type": "function", "function": {"name": "get_time"}})
    filtered, force = resolve_effective_tools([WEATHER, TIME], tc)
    assert filtered == [TIME] and force is True
