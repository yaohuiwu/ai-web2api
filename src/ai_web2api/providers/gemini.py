"""Gemini（gemini.google.com）驱动。

纯配置驱动：复用通用 ``WebChatProvider`` 引擎。

实测（2026-09，**游客态，无需登录**）：
    composer   div.ql-editor[contenteditable=true]（Quill 编辑器）
    send       button[aria-label="发送"]
    正文       .model-response-text（count=1，纯正文；用户侧是 ``user-query``，不会误匹配）
    模型       游客态当前为 **Flash-Lite** —— 与"额度用尽后自动降级为 Flash-Lite、仍可免费聊"一致
    session    https://gemini.google.com/app/<hex id>

要点：
- **游客态即可用**（不需要登录、没有验证码）→ 因此 ``login_check`` 留空（用输入框判定"可用"），
  且**不配** ``logged_out``：游客态是合法可用状态，不能当"未登录"拒服务（与豆包相反）。
- 登录（Google 账号）只能**手动**，但**非必需**；登录后才能保存历史/使用更多能力。
  CLI 自动检测见 ``config.yaml`` 的 ``login.detect``：**不能用裸头像选择器**——游客态顶栏
  的占位头像 ``<img class="user-icon" alt="个人资料照片" src=".../default-user">`` 在未登录时
  也会命中，会误判成已登录（未登录就保存 state）；必须叠加「无登录链接」条件或只认 SignOutOptions。
- 改版需校准：``stop_button``（生成中未观测到）与上传入口（``input[type=file]``）尚未实测。
"""

from __future__ import annotations

from .webchat import WebChatProvider


class GeminiProvider(WebChatProvider):
    """Gemini：通用引擎 + ``config.yaml`` 选择器。"""

    # 会话 URL：https://gemini.google.com/app/<hex id>
    session_url_pattern = r"/app/([0-9a-fA-F]{8,})"
