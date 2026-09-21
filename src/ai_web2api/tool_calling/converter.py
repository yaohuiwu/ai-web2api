"""Function Calling —— 消息/工具 ↔ 单段 prompt，以及输出解析回标准 tool_calls。

参考 token-free-gateway 的 ``tool-calling/converter.ts``：

- ``resolve_effective_tools``：按 ``tool_choice`` 过滤工具 + 是否强制使用
- ``build_prompt``：无状态/create —— 工具说明 + 角色转录（含工具结果）
- ``build_resume_prompt``：thread resume —— 只发本轮（工具结果回合注入 <tool_result>）
- ``parse_tool_response``：模型文本 → ``(content|None, tool_calls|None, finish_reason)``
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .parser import extract_tool_calls, has_tool_call
from .prompt import build_tool_prompt, detect_language


# ---------- tools 归一化 ----------

def _fn(tool: Any) -> dict:
    """取 OpenAI 工具包装里的 function 部分（dict / Pydantic 均可）。"""
    if isinstance(tool, dict):
        fn = tool.get("function")
        return fn if isinstance(fn, dict) else tool
    fn = getattr(tool, "function", None)
    if fn is None:
        return {}
    return fn.model_dump() if hasattr(fn, "model_dump") else dict(fn)


def normalize_tool_defs(tools: list[Any] | None) -> list[dict]:
    out = []
    for t in tools or []:
        fn = _fn(t)
        out.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {},
            }
        )
    return out


def tool_names(tools: list[Any] | None) -> set[str]:
    return {d["name"] for d in normalize_tool_defs(tools) if d["name"]}


def _choice_kind(tool_choice: Any) -> tuple[str, str | None]:
    """→ (kind, name)；kind ∈ auto/none/required/function。"""
    if tool_choice is None:
        return "auto", None
    if isinstance(tool_choice, str):
        return tool_choice, None
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        return "function", fn.get("name") if isinstance(fn, dict) else None
    fn = getattr(tool_choice, "function", None)
    return "function", getattr(fn, "name", None)


def resolve_effective_tools(
    tools: list[Any] | None, tool_choice: Any
) -> tuple[list[Any], bool]:
    """按 ``tool_choice`` 得到生效工具 + 是否强制使用。"""
    tools = list(tools or [])
    if not tools:
        return [], False
    kind, name = _choice_kind(tool_choice)
    if kind == "none":
        return [], False
    if kind == "required":
        return tools, True
    if kind == "function":
        filtered = [t for t in tools if _fn(t).get("name") == name]
        return filtered, bool(filtered)
    return tools, False


# ---------- 消息转录 ----------

def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(p.get("text", "")) if isinstance(p, dict) and p.get("type") in ("text", "input_text")
            else (str(p) if not isinstance(p, dict) else "")
            for p in content
        )
    return "" if content is None else str(content)


def _detect_lang(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return detect_language(_text_content(m.get("content")))
    return "en"


def _format_assistant(m: dict) -> str | None:
    calls = m.get("tool_calls")
    if calls:
        blocks = []
        for tc in calls:
            fn = tc.get("function", tc) if isinstance(tc, dict) else _fn(tc)
            args = fn.get("arguments") or "{}"
            blocks.append(f'```tool_json\n{{"tool":"{fn.get("name", "")}","parameters":{args}}}\n```')
        return "Assistant: [Called tools]\n" + "\n".join(blocks)
    text = _text_content(m.get("content"))
    return f"Assistant: {text}" if text else None


def _format_tool_result(tool_call_id: str, content: str) -> str:
    return f'<tool_result tool_call_id="{tool_call_id}">\n{content}\n</tool_result>'


def format_message(m: dict) -> str | None:
    role = m.get("role")
    if role in ("system", "developer"):
        return f"System: {_text_content(m.get('content'))}"
    if role == "user":
        return f"Human: {_text_content(m.get('content'))}"
    if role == "assistant":
        return _format_assistant(m)
    if role == "tool":
        return _format_tool_result(str(m.get("tool_call_id") or "unknown"), _text_content(m.get("content")))
    if role == "function":  # 旧式 OpenAI function 结果
        return _format_tool_result(str(m.get("name") or "unknown"), _text_content(m.get("content")))
    return None


_CONT_CN = "请根据以上工具执行结果回答用户的问题。"
_CONT_EN = "Please answer the user's question based on the tool results above."


def _is_tool_result(m: dict | None) -> bool:
    return bool(m and m.get("role") in ("tool", "function"))


# ---------- 组装 prompt ----------

def build_prompt(messages: list[dict], tools: list[Any] | None = None, tool_choice: Any = None) -> str:
    """无状态 / thread create：工具说明 + 全量历史转录。"""
    effective, force = resolve_effective_tools(tools, tool_choice)
    lang = _detect_lang(messages)
    parts: list[str] = []
    if effective:
        parts.append(build_tool_prompt(normalize_tool_defs(effective), lang, force))
    for m in messages:
        s = format_message(m)
        if s:
            parts.append(s)
    if _is_tool_result(messages[-1] if messages else None):
        parts.append(_CONT_CN if lang == "cn" else _CONT_EN)
    return "\n\n".join(parts)


def build_resume_prompt(
    messages: list[dict], tools: list[Any] | None = None, tool_choice: Any = None
) -> str:
    """thread resume：页面已有历史，只发「本轮」（工具结果回合注入 <tool_result>）。"""
    effective, force = resolve_effective_tools(tools, tool_choice)
    lang = _detect_lang(messages)
    parts: list[str] = []
    if effective:
        # resume 也带一次工具说明，保证模型知道输出格式
        parts.append(build_tool_prompt(normalize_tool_defs(effective), lang, force))
    last = messages[-1] if messages else None
    if last is None:
        return "\n\n".join(parts)
    if _is_tool_result(last):
        s = format_message(last)
        if s:
            parts.append(s)
        parts.append(_CONT_CN if lang == "cn" else _CONT_EN)
    elif last.get("role") == "user":
        parts.append(_text_content(last.get("content")))
    else:
        s = format_message(last)
        if s:
            parts.append(s)
    return "\n\n".join(parts)


def needs_tool_handling(messages: list[dict], tools: list[Any] | None) -> bool:
    """该请求是否需要走 FC 路径（有 tools，或本轮是工具结果）。"""
    return bool(tools) or _is_tool_result(messages[-1] if messages else None)


# ---------- 响应解析 ----------

def parse_tool_response(
    text: str, tools: list[Any] | None
) -> tuple[str | None, list[dict] | None, str]:
    """模型文本 → ``(content|None, tool_calls|None, finish_reason)``。

    - 未提供工具 / 无工具调用 / 工具名都不在请求内 → 当普通文本（finish_reason="stop"）
    - 命中 → ``content=None``、``finish_reason="tool_calls"``（对齐 OpenAI）
    """
    names = tool_names(tools)
    if not names or not has_tool_call(text):
        return text, None, "stop"
    valid = [c for c in extract_tool_calls(text) if c.name in names]
    if not valid:
        return text, None, "stop"
    calls = [
        {
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": c.name,
                "arguments": json.dumps(c.arguments, ensure_ascii=False),
            },
        }
        for c in valid
    ]
    return None, calls, "tool_calls"
