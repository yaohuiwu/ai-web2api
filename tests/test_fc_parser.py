"""Function Calling 输出解析（Step 2）。

运行：.venv/bin/python -m pytest tests/test_fc_parser.py -v
"""

from __future__ import annotations

from ai_web2api.tool_calling.parser import (
    extract_single_tool_call,
    extract_tool_calls,
    has_tool_call,
)


def test_fenced_tool_json():
    text = '好的。\n\n```tool_json\n{"tool":"web_search","parameters":{"query":"bun runtime"}}\n```'
    assert extract_single_tool_call(text) == _call("web_search", {"query": "bun runtime"})


def test_bare_json():
    text = 'I need to run. {"tool":"exec","parameters":{"command":"ls -la"}}'
    assert extract_single_tool_call(text) == _call("exec", {"command": "ls -la"})


def test_xml_tool_call():
    text = '<tool_call name="read">{"name":"read","arguments":{"path":"/tmp/a.txt"}}</tool_call>'
    assert extract_single_tool_call(text) == _call("read", {"path": "/tmp/a.txt"})


def test_openai_style_wrapper():
    text = '{"tool_calls":[{"name":"exec","arguments":{"command":"pwd"}}]}'
    assert extract_single_tool_call(text) == _call("exec", {"command": "pwd"})


def test_truncated_json_fuzzy_repair():
    text = '{"tool":"exec","parameters":{"command":"ls"}'
    assert extract_single_tool_call(text) == _call("exec", {"command": "ls"})


def test_fenced_name_arguments_form():
    text = '```tool_json\n{"name":"get_weather","arguments":{"city":"Tokyo"}}\n```'
    assert extract_single_tool_call(text) == _call("get_weather", {"city": "Tokyo"})


def test_plain_text_returns_none():
    assert extract_single_tool_call("你好，这是普通回答。") is None


def test_multiple_xml_calls():
    text = (
        '<tool_call>{"name":"read","arguments":{"path":"a.txt"}}</tool_call>\n'
        '<tool_call>{"name":"read","arguments":{"path":"b.txt"}}</tool_call>'
    )
    calls = extract_tool_calls(text)
    assert [c.name for c in calls] == ["read", "read"]
    assert calls[0].arguments == {"path": "a.txt"}


def test_single_wrapped_as_array():
    text = '```tool_json\n{"tool":"exec","parameters":{"command":"ls"}}\n```'
    calls = extract_tool_calls(text)
    assert len(calls) == 1 and calls[0].name == "exec"


def test_no_call_empty_array():
    assert extract_tool_calls("no tools here") == []


def test_has_tool_call():
    assert has_tool_call('{"tool":"x","parameters":{}}') is True
    assert has_tool_call("<tool_call>x</tool_call>") is True
    assert has_tool_call("just text") is False


def _call(name: str, args: dict):
    from ai_web2api.tool_calling.parser import ParsedToolCall

    return ParsedToolCall(name, args)
