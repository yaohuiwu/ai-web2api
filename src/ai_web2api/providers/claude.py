"""Claude（claude.ai）驱动。

纯配置驱动：复用通用 ``WebChatProvider`` 引擎，本类只做注册、站点说明与
**SSE 解析**（``network.url_pattern`` 配在 ``config.yaml``）。

**仅手动登录**（Google / 邮箱验证码，无密码自动登录）→ ``login.mode: manual``。
未登录访问 ``/new`` 会跳转 ``/login``（无 composer）→ ``login_check`` 留空，
用输入框判定即可（无需反向标记）。

实测（2026-09，登录态；Chromium 经宿主代理 + 已导入 state）：
    composer   div.ProseMirror[contenteditable=true]（ProseMirror）→ ``type_prompt: true``
    send       button[aria-label="Send message"]（英文 UI；``locale: en-US``）
    stop       button[aria-label*="Stop"]（生成中出现、结束消失）
    正文       .font-claude-response（**干净正文**；``[data-testid="assistant-message"]``
               的 innerText 会混入无障碍文案 "Claude responded: …"，不要直接用）
    完成       button[data-testid="action-bar-copy"]（该条消息工具栏出现 == 写完）
    session    https://claude.ai/chat/<uuid>
    SSE        POST /api/organizations/<org>/chat_conversations/<id>/completion
               event: content_block_delta / data: {"type":"content_block_delta",
               "delta":{"type":"text_delta","text":"…"}}
               （扩展思考为 ``thinking_delta`` / ``thinking``）

取舍：markdown 重渲染频繁 → ``stream_content: false`` + ``preview_stream``（DOM 兜底），
网络通道为主（增量 diff）；SSE 解析见 :meth:`_parse_sse_snapshot`。
"""

from __future__ import annotations

import json

from .webchat import WebChatProvider


class ClaudeProvider(WebChatProvider):
    """Claude：通用引擎 + ``config.yaml`` 选择器 + SSE 解析。"""

    # 会话 URL：https://claude.ai/chat/<uuid>
    session_url_pattern = r"/chat/([0-9a-fA-F-]{8,})"

    # 发送后 Claude 会立刻渲染空助手壳，但 SSE 首帧 ~1s 才到 → 给网络通道 3s 优先时间
    NET_FIRST_GRACE = 3.0

    def _net_stream_complete(self, sse: str) -> bool:
        """Claude 发完 ``message_stop`` 后**不关连接**（继续推 ping）→ 用它作结束标记。"""
        return "message_stop" in sse

    @classmethod
    def _parse_sse_snapshot(cls, sse_text: str) -> tuple[str, str]:
        """Claude SSE → (thinking, content)。

        逐块取 ``data:`` JSON，累计 ``content_block_delta``：
        - ``delta.type == "text_delta"`` → 正文（``delta.text``）
        - ``delta.type == "thinking_delta"`` → 思考（``delta.thinking``）
        其余事件（ping / message_start / content_block_start/stop / message_delta /
        message_stop / input_json_delta 等）忽略。
        """
        if not sse_text:
            return "", ""
        thinking_parts: list[str] = []
        content_parts: list[str] = []
        for block in sse_text.replace("\r\n", "\n").split("\n\n"):
            data = None
            for line in block.split("\n"):
                if line.startswith("data:"):
                    data = (data or "") + line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:  # noqa: BLE001  不完整的尾块/非 JSON 事件
                continue
            if not isinstance(obj, dict) or obj.get("type") != "content_block_delta":
                continue
            delta = obj.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "thinking_delta":
                piece = delta.get("thinking") or ""
                if piece:
                    thinking_parts.append(piece)
            elif dtype == "text_delta":
                piece = delta.get("text") or ""
                if piece:
                    content_parts.append(piece)
            elif delta.get("text"):
                content_parts.append(delta["text"])
        return "".join(thinking_parts).strip(), "".join(content_parts).strip()
