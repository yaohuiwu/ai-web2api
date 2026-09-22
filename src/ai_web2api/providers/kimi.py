"""Kimi（kimi.com）驱动。

纯配置驱动：复用通用 ``WebChatProvider`` 引擎，本类只做注册 + 站点说明（暂无特有逻辑）。

选择器实测于 2026-09（**未登录状态**，见 docs/PROVIDER_KIMI.md）：
    composer   div.chat-input-editor[contenteditable=true]（``type_prompt: true`` 逐字输入有效）
    send       div.send-button-container
    attach     input.hidden-input[type=file]（multiple）
    messages   .message-list / .message-list-container
    login      仅「微信扫码」/「手机号 + 验证码」（带易盾验证码）→ ``login.mode: manual``

**待校准**（需登录后实测）：``response_container`` / ``login_check`` / ``stop_button``。
未登录时 composer 也可输入（游客可用），因此 ``login_check`` 不校准会把「未登录」误判为已登录。
"""

from __future__ import annotations

from .webchat import WebChatProvider


class KimiProvider(WebChatProvider):
    """Kimi：通用引擎 + ``config.yaml`` 选择器。"""
