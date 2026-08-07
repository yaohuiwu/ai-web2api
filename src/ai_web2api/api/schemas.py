"""OpenAI 兼容的请求/响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    """OpenAI 兼容消息。role 放宽到全部已知角色（llama_index 会发 developer/tool）。

    content 兼容 str 与多部分列表（[{type:text, text:...}, {type:image_url,...}]）；
    其余字段（tool_calls/tool_call_id/name 等）默认忽略，不参与校验。
    """

    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "user", "assistant", "developer", "tool", "function"] = "user"
    content: str | list[Any] = ""


def normalize_message(m: dict) -> dict:
    """把 OpenAI 消息归一化成驱动层可用的 {role, content: str}。

    - developer → system（OpenAI 语义：developer 是 system 的替代）
    - content 为多部分列表时提取 text 部分；图片/未知类型保留占位
    """
    role = m.get("role", "user")
    if role == "developer":
        role = "system"
    content = m.get("content", "")
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict):
                ptype = p.get("type")
                if ptype in ("text", "input_text"):
                    parts.append(str(p.get("text", "")))
                elif ptype == "image_url":
                    parts.append("[图片]")
                else:
                    parts.append("[内容]")
            else:
                parts.append(str(p))
        content = "\n".join(parts)
    else:
        content = str(content)
    return {"role": role, "content": content}


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    thread_id: str | None = None  # 会话绑定：同 id 复用同一 Web 页面多轮；缺省 = 无状态新会话
    # 以下字段在 Web 端不可控，收到不报错、仅忽略：
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: Any = None
    n: int | None = None


class ResponseMessage(BaseModel):
    role: str = "assistant"
    content: str = ""
    reasoning_content: str | None = None


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    thread_id: str | None = None  # 会话绑定时回显；无状态请求为 null
    choices: list[ChatCompletionChoice]
    usage: dict[str, Any] = Field(default_factory=dict)


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "ai-web2api"


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
