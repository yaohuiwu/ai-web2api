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


def _extract_attachments(content: list) -> tuple[list[str], list[dict]]:
    """从多部分 content 提取图片附件。

    OpenAI 标准 image_url part：{"type": "image_url", "image_url": {"url": "..."}}
    - data URL（data:image/png;base64,...）→ {"type": "image", "mime", "data"(base64), "name"}
    - http(s) URL → {"type": "image", "mime"(缺省 image/jpeg), "url", "name"}
    返回 (剩余文本 parts, 附件列表)。
    """
    parts: list[str] = []
    atts: list[dict] = []
    for p in content:
        if isinstance(p, dict):
            ptype = p.get("type")
            if ptype in ("text", "input_text"):
                parts.append(str(p.get("text", "")))
            elif ptype == "image_url":
                iu = p.get("image_url") or {}
                url = iu.get("url") if isinstance(iu, dict) else iu
                if isinstance(url, str) and url:
                    atts.append(_parse_image_url(url))
            else:
                parts.append("[内容]")
        else:
            parts.append(str(p))
    return parts, atts


def _parse_image_url(url: str) -> dict:
    """解析图片 URL 为附件描述：data URL 提取 mime+base64；http(s) 保留 url 待下载。"""
    name = "image"
    if url.startswith("data:"):
        head, _, b64 = url.partition(",")
        mime = head[5:].split(";")[0] or "image/png"
        ext = mime.split("/")[-1] if "/" in mime else "png"
        if ext == "jpeg":
            ext = "jpg"
        return {"type": "image", "mime": mime, "data": b64, "name": f"image.{ext}"}
    # http(s)：从 URL 猜文件名
    import urllib.parse
    base = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    if base:
        name = base
    return {"type": "image", "mime": "image/jpeg", "url": url, "name": name}


def normalize_message(m: dict) -> tuple[dict, list[dict]]:
    """把 OpenAI 消息归一化成驱动层可用的 {role, content: str}，并提取图片附件。

    - developer → system（OpenAI 语义：developer 是 system 的替代）
    - content 为多部分列表时提取 text 部分拼接；image_url part 提取为附件
      （返回的 msg 不含图片；附件由 driver 上传到 Web 端输入框）
    - 返回 (msg, attachments)；无附件时 attachments 为空列表
    """
    role = m.get("role", "user")
    if role == "developer":
        role = "system"
    content = m.get("content", "")
    atts: list[dict] = []
    if isinstance(content, list):
        parts, atts = _extract_attachments(content)
        content = "\n".join(parts)
    else:
        content = str(content)
    return {"role": role, "content": content}, atts


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    thread_id: str | None = None  # 会话绑定：同 id 复用同一 Web 页面多轮；缺省 = 无状态新会话
    # Web 端选项（provider 通用，OpenAI 原生客户端用 extra_body 传）：
    mode: str | None = None       # 模式：fast/expert/image 等（provider 映射自己的 UI）；仅新会话生效
    deep_think: bool | None = None  # 深度思考开关（每次请求生效；None = 不改页面状态）
    search: bool | None = None      # 智能搜索开关（每次请求生效；None = 不改页面状态）
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
