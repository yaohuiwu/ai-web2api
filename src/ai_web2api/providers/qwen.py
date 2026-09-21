"""Qwen Chat (chat.qwen.ai) 驱动。

通用聊天流程在 :class:`WebChatProvider`；Qwen 的差异（模型下拉、模式下拉、
附件入口、登录页）全部走配置（见 ``config.yaml`` 的 ``providers.qwen``），
本类目前只登记 name / 会话 URL 形态；实测到新差异时在此覆写钩子。
"""

from __future__ import annotations

import logging

from .webchat import WebChatProvider

logger = logging.getLogger(__name__)


class QwenProvider(WebChatProvider):
    """chat.qwen.ai 驱动。"""

    name = "qwen"
    # 会话 URL：chat.qwen.ai/c/<uuid>（实测）
    session_url_pattern = r"/c/([0-9a-fA-F-]{8,})"
