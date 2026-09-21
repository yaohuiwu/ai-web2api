"""Function Calling —— 从网页模型文本响应中解析 tool calls（多格式容错）。

参考 token-free-gateway 的 ``tool-calling/parser.ts``。按序尝试：
1. 围栏代码块 ```` ```tool_json {...} ``` ````
2. OpenAI 风格 ``{"tool_calls":[{"name":..,"arguments":..}]}``
3. 裸 JSON ``{"tool":..,"parameters":{..}}``
4. XML ``<tool_call>{"name":..,"arguments":..}</tool_call>``（支持多个）
5. 截断 JSON 自动补右括号
兼容 ``tool`` / ``name`` 两种键名、``parameters`` / ``arguments`` 两种入参键。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


@dataclass
class ParsedToolCall:
    name: str
    arguments: dict = field(default_factory=dict)


_FENCED = re.compile(r"```tool_json\s*\n?\s*(\{[\s\S]*\})\s*\n?\s*```")
_BARE = re.compile(
    r'\{\s*"tool"\s*:\s*"([^"]+)"\s*,\s*"parameters"\s*:\s*(\{[\s\S]*?\})\s*\}'
)
_XML = re.compile(r"<tool_call[^>]*>([\s\S]*?)</tool_call>")
_OPENAI_CALLS = re.compile(
    r'\{\s*"tool_calls"\s*:\s*\[\s*(\{[\s\S]*?\})\s*(?:,[\s\S]*?)?\]\s*\}'
)
_FUZZY = re.compile(
    r'\{\s*"tool"\s*:\s*"([^"]+)"\s*,\s*"parameters"\s*:\s*\{([^}]*)\}'
)


def parse_tool_json(raw: str) -> ParsedToolCall | None:
    """解析一段 JSON 文本为 ParsedToolCall；自动补齐缺失的右括号。"""
    try:
        cleaned = raw.strip()
        opens = cleaned.count("{")
        closes = cleaned.count("}")
        if opens > closes:
            cleaned += "}" * (opens - closes)
        obj = json.loads(cleaned)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if isinstance(obj.get("tool"), str):
        args = obj.get("parameters") or {}
        return ParsedToolCall(obj["tool"], args if isinstance(args, dict) else {})
    if isinstance(obj.get("name"), str):
        args = obj.get("arguments") or {}
        return ParsedToolCall(obj["name"], args if isinstance(args, dict) else {})
    return None


def extract_single_tool_call(text: str) -> ParsedToolCall | None:
    """按优先级提取单个 tool call；都不是则返回 None。"""
    m = _FENCED.search(text)
    if m and m.group(1):
        return parse_tool_json(m.group(1))

    m = _OPENAI_CALLS.search(text)
    if m and m.group(1):
        return parse_tool_json(m.group(1))

    m = _BARE.search(text)
    if m and m.group(1) and m.group(2) is not None:
        try:
            args = json.loads(m.group(2))
        except Exception:
            return None
        return ParsedToolCall(m.group(1), args if isinstance(args, dict) else {})

    m = _XML.search(text)
    if m and m.group(1):
        return parse_tool_json(m.group(1))

    # 模糊修复：截断/嵌套不完整的 parameters
    m = _FUZZY.search(text)
    if m and m.group(1):
        repaired = f'{{"tool":"{m.group(1)}","parameters":{{{m.group(2)}}}}}'
        return parse_tool_json(repaired)

    return None


def extract_tool_calls(text: str) -> list[ParsedToolCall]:
    """提取全部 tool calls：优先多个 XML，其次单个。"""
    xml_matches = _XML.findall(text)
    if xml_matches:
        calls = [c for raw in xml_matches if (c := parse_tool_json(raw)) is not None]
        if calls:
            return calls
    single = extract_single_tool_call(text)
    return [single] if single else []


def has_tool_call(text: str) -> bool:
    """文本中是否疑似包含 tool call（用于短路，避免无谓解析）。"""
    return bool(
        _FENCED.search(text)
        or _BARE.search(text)
        or _XML.search(text)
        or _OPENAI_CALLS.search(text)
    )
