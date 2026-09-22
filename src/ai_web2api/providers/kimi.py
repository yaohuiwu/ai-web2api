"""Kimi（kimi.com）驱动。

纯配置驱动：复用通用 ``WebChatProvider`` 引擎，本类只做注册 + 站点说明（暂无特有逻辑）。

选择器实测于 2026-09（**未登录状态**，见 docs/PROVIDER_KIMI.md）：
    composer   div.chat-input-editor[contenteditable=true]（``type_prompt: true`` 逐字输入有效）
    send       div.send-button-container
    attach     input.hidden-input[type=file]（multiple）
    messages   .message-list / .message-list-container
    login      仅「微信扫码」/「手机号 + 验证码」（带易盾验证码）→ ``login.mode: manual``

已校准（2026-09 实测，登录态，发真实消息验过）：
    response   .chat-content-item-assistant .markdown   ← 助手正文**取最后一个** = 纯答案
               （`.segment-content` 会把「思考已完成…」混进正文，故不单独用它）
    login      .user-area__main img（登录后才有头像；未登录能输入，故必须靠它判定）
    session    https://www.kimi.com/chat/<uuid>

已知取舍：
    - 正文与思考同处一个 segment → ``stream_content: false``（缓冲后一次发），避免把思考当正文；
    - 站点**没有停止按钮** → ``stop_button: []``，靠 ``stable_polls`` 判定结束（稍慢）；
    - 登录态 token 在 **localStorage**（``refresh_token`` JWT，实测约 90 天），cookie 里没有 →
      UI「认证有效期」显示「未知」；登录只有微信扫码/手机号+验证码（易盾），故 ``login.mode: manual``。
"""

from __future__ import annotations

from .webchat import WebChatProvider


class KimiProvider(WebChatProvider):
    """Kimi：通用引擎 + ``config.yaml`` 选择器。"""

    # 会话 URL：https://www.kimi.com/chat/<uuid>
    session_url_pattern = r"/chat/([0-9a-fA-F-]{8,})"
