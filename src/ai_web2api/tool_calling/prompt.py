"""Function Calling —— 把 tools 定义注入成提示词。

参考 token-free-gateway 的 ``tool-calling/prompt.ts``：工具清单 + 输出格式约定 +
一个 few-shot 示例；支持中/英；``force_use``（tool_choice=required）时加强制语。
"""

from __future__ import annotations

import json
import re


def detect_language(text: str) -> str:
    """按中文字符占比猜语言：``cn`` / ``en``。"""
    cn = len(re.findall(r"[\u4e00-\u9fff]", text))
    return "cn" if cn and cn > len(text) * 0.1 else "en"


def _defs_json(defs: list[dict]) -> str:
    """normalized defs ``[{name,description,parameters}]`` → 紧凑 JSON 文本。"""
    return json.dumps(
        [
            {
                "name": d.get("name", ""),
                "description": d.get("description") or "",
                "parameters": d.get("parameters") or {},
            }
            for d in defs
        ],
        ensure_ascii=False,
        indent=2,
    )


_EXAMPLE_CN = """示例: 要给数字5加1，只返回:
```tool_json
{"tool":"plus_one","parameters":{"number":"5"}}
```
(plus_one仅为示例，非真实工具)"""

_EXAMPLE_EN = """Example: to add 1 to number 5, return ONLY:
```tool_json
{"tool":"plus_one","parameters":{"number":"5"}}
```
(plus_one is just an example, not a real tool)"""


def build_tool_prompt(defs: list[dict], lang: str = "en", force_use: bool = False) -> str:
    """生成工具说明段（会被拼到 prompt 最前面）。"""
    defs_text = _defs_json(defs)
    if lang == "cn":
        force = "\n\n重要：你必须使用上述工具之一来回应。请不要直接用文字回答，必须调用工具。" if force_use else ""
        return (
            "你可以使用以下工具。当需要使用工具时，只返回tool_json代码块，不要包含其他文字。\n\n"
            f"可用工具:\n{defs_text}\n\n{_EXAMPLE_CN}\n\n"
            f"需要使用工具时，只返回一个tool_json块。不需要工具则直接回答。{force}\n\n"
        )
    force = "\n\nIMPORTANT: You MUST use one of the tools above. Do NOT answer with plain text." if force_use else ""
    return (
        "You have access to the following tools. When you need to use a tool, "
        "reply ONLY with a tool_json code block, no other text.\n\n"
        f"Available tools:\n{defs_text}\n\n{_EXAMPLE_EN}\n\n"
        f"To use a tool, reply with exactly one tool_json block. "
        f"If no tool is needed, answer directly.{force}\n\n"
    )
