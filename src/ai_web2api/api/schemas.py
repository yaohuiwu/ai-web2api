"""OpenAI 兼容的请求/响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = ""


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
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
