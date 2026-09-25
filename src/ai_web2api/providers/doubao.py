"""豆包（doubao.com）驱动。

纯配置驱动：复用通用 ``WebChatProvider`` 引擎，本类只做注册 + 站点说明。

**仅手动登录**（无密码登录：手机号验证码/扫码），且**容器内会提示「受区域限制，请先登录再使用豆包」**
→ 未登录时拿不到输入框，无法用游客态校准。

实测（2026-09，游客页 / 宿主浏览器）：
    composer   div.tiptap.ProseMirror[contenteditable=true]（tiptap/ProseMirror，无 textarea）
    消息容器   .message-container / .chat-item（游客页 DOM 可见，登录后结构待确认）
    登录入口   .login-button（右上「登录」）

结束信号（按优先级）：
    1. 网络请求结束（XHR + fetch 拦截，`network.url_pattern`）→ 立即 DOM 提取
    2. 停止按钮消失（生成中右下角出现，结束消失）
    3. 稳定性兜底（`stable_polls*2`，降级使用）
"""

from __future__ import annotations

from .webchat import WebChatProvider


class DoubaoProvider(WebChatProvider):
    """豆包：通用引擎 + ``config.yaml`` 选择器。"""

    # 会话 URL 形如 https://www.doubao.com/chat/<数字 id>（待登录后确认；不匹配时不影响主流程）
    session_url_pattern = r"/chat/(\d{6,})"
