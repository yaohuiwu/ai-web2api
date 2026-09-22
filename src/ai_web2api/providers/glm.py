"""智谱清言（chatglm.cn）驱动。

纯配置驱动：复用通用 ``WebChatProvider`` 引擎。

**注意：站点前置阿里云 WAF「滑动验证」** —— headless 与 headful 实测都被拦在
「访问验证：请按住滑块，拖动到最右边」页面，**DOM 自动化无法通过**。
建议流程：在**有显示器的宿主**执行 ``./scripts/login.sh glm``（人工拖滑块 + 登录），
再把 ``storage_state``（含 WAF cookie）导入服务；可行性与存活时间需实测确认。

实测（2026-09）：
    headless / headful 均落到「滑动验证页面」，无 composer
    登录方式：无密码（手机号验证码/扫码）→ ``login.mode: manual``
**待校准**：WAF 通过后的 ``input`` / ``send_button`` / ``response_container`` / ``login_check``。
"""

from __future__ import annotations

from .webchat import WebChatProvider


class GlmProvider(WebChatProvider):
    """智谱清言：通用引擎 + ``config.yaml`` 选择器。"""

    # 会话 URL 形态未确认（alltoolsdetail 带 query）→ 暂不启用 thread 恢复
    session_url_pattern = None
