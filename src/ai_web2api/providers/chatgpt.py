"""ChatGPT (chatgpt.com) 驱动。

与其他 provider 一样，通用流程在 :class:`WebChatProvider`，站点差异（选择器、模型菜单、
附件入口）全部走 ``config.yaml`` 的 ``providers.chatgpt``。本类只登记 name / 会话 URL 形态。

注意：
- **无密码自动登录** → ``login.mode: manual``，用 ``python -m ai_web2api.cli login chatgpt``
  或 ``/ui`` 导入 ``state.json``。
- 输入框是 contenteditable（ProseMirror），配置里需 ``selectors.type_prompt: true``。
- 强 Cloudflare：``cf_clearance`` 与 IP+UA 绑定，登录与请求需同一出口/UA。
"""

from __future__ import annotations

import logging

from .webchat import WebChatProvider

logger = logging.getLogger(__name__)


class ChatGPTProvider(WebChatProvider):
    """chatgpt.com 驱动。"""

    name = "chatgpt"
    # 会话 URL: chatgpt.com/c/<uuid>（新 UI 常见 `WEB:<uuid>` 前缀）
    session_url_pattern = r"/c/([0-9A-Za-z:_-]{8,})"
