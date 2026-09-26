"""元宝（yuanbao.tencent.com）驱动。

纯配置驱动：复用通用 ``WebChatProvider`` 引擎，本类只做注册 + 站点说明。

实测（2026-09，**登录态**）：
    composer   div.ql-editor[contenteditable=true]（Quill 编辑器）
    发送       #yuanbao-send-btn（DIV，aria-label="发送"）
    正文       .hyc-content-md .hyc-common-markdown（流式渐进，可 diff）
    思考       .hyc-component-deep-search-agent__think-container
    停止       [aria-label*="停止"]（生成中出现、结束消失）
    登录       微信扫码 / 手机 / QQ（无密码自动登录）→ login.mode: manual
    代理       国内站点直连（proxy: false）
    流式端点   POST /api/chat/<conversationId>（SSE，仅观测，不解析）

要点：
- **必须登录**：游客态被 ``hyc-login-v2`` 遮罩拦截，输入框 placeholder
  =「请登录后输入内容」（与 Gemini 相反）。
- **登录标记**：登录后右上角是头像 ``.yb-common-nav__ft__avatar``；未登录时
  右上角是「登录」按钮（``button.agent-dialogue__tool__login``）、左下角是
  「未登录」。**不能用 input**（未登录也有），也**不能用宽泛 ``[class*=avatar]``**
  （会命中未登录时的骨架 ``.yb-nav__user-skeleton__avatar``）。
- **思考 vs 正文**：``.hyc-component-deep-search-agent`` 会把**正文也包住** →
  绝不能拿它当思考容器（否则正文被当思考、content 为空）。真正的思考文本在
  ``.hyc-component-deep-search-agent__think-container``。
"""

from __future__ import annotations

from .webchat import WebChatProvider


class YuanbaoProvider(WebChatProvider):
    """元宝：通用引擎 + ``config.yaml`` 选择器。"""

    # 会话 URL：https://yuanbao.tencent.com/chat/<agentId>/<chatId>
    session_url_pattern = r"/chat/[^/]+/([0-9a-fA-F-]{8,})"
