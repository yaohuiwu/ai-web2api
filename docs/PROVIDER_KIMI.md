# Kimi（kimi.com）接入

> 状态：**可用**（`enabled: true`）。选择器已**实测校准**，并端到端验证过（正文提取、思考分离、会话复用）。
> 驱动：`src/ai_web2api/providers/kimi.py`（`KimiProvider`，纯配置驱动，复用通用 `WebChatProvider`）。

## 1. 实测结论（2026-09，登录态）

| 项 | 结果 |
|---|---|
| 站点 | `kimi.moonshot.cn` / `kimi.com` → 302 到 **`https://www.kimi.com/`** |
| 登录 | **微信扫码 / 手机号 + 验证码**（带**易盾验证码** `yidun_input`，无密码登录）→ 只能 `login.mode: manual` |
| 输入框 | `div.chat-input-editor[contenteditable=true]`（无 textarea）；逐字输入有效 → `type_prompt: true` |
| 发送 | `div.send-button-container`（输入后出现） |
| 附件 | `input.hidden-input[type=file][multiple]` |
| **正文** | `.chat-content-item-assistant .markdown-container:not(.toolcall-content-text) .markdown` |
| 思考 | 同一条消息里 **带** `toolcall-content-text` 的那个 `.markdown-container`（另有 `.toolcall-rollup`） |
| 结束判定 | **站点没有停止按钮** → `stop_button: []`，靠 `stable_polls` 稳定性判定（短回答约 20s 返回） |
| 会话 URL | `https://www.kimi.com/chat/<uuid>` → `session_url: "{base}/chat/{id}"`，`session_url_pattern = r"/chat/([0-9a-fA-F-]{8,})"` |
| 登录判定 | `.user-area__main img`（登录后才有头像）。**未登录也能在输入框输入**（游客），所以不能用输入框判登录 |

端到端验证（真登录 + 真消息）：

```
POST /v1/chat/completions {"model":"kimi-web",...}  → content='收到'（思考单独进 reasoning_content，不混正文）
thread_id 复用：第 1 轮"记住 7" → 第 2 轮只发最后一条 → 正确答 '7'（且会话 URL 已落库）
```

## 2. 已知取舍

- **正文为缓冲发送**（`stream_content: false`）：Kimi 把思考与正文放在同一个 segment，逐字 diff 会把「思考已完成…」当正文发出去；缓冲后一次发更稳（代价：没有逐字流式观感）。
- **无停止按钮**：结束判定靠 `stable_polls`（当前 20）× 3 + `min_wait_before_stable`，短回答约 20s；想更快可调小 `stable_polls`（有截断风险）。
- **认证有效期显示「未知」**：Kimi 的登录态在 **localStorage**（`access_token` 几分钟、`refresh_token` JWT **实测约 90 天**），cookie 里没有可判定的过期时间；UI 的 `auth_cookies` 只读 cookie，故显示未知。
- 不逆向内部 API：与其它 provider 一样只走 DOM 选择器，改版只需改配置。

## 3. 改版后如何重新校准（约 5 分钟）

```bash
# 1) 发一条测试消息，dump 响应区 DOM（现有调试接口）
curl -X POST http://127.0.0.1:8000/admin/kimi/debug/probe \
  -H 'Content-Type: application/json' -d '{"message": "只回复两个字：收到"}'
# 2) 逐个验证候选选择器（返回数 > 0 即命中；注意区分「思考」与「正文」两个 .markdown-container）
curl 'http://127.0.0.1:8000/admin/kimi/debug/dom?selector=.chat-content-item-assistant%20.markdown'
# 3) 登录态判定：登录前后各看一眼，挑「只在登录后出现」的
curl 'http://127.0.0.1:8000/admin/kimi/debug/dom?selector=.user-area__main'
# 4) 改 config.yaml → providers.kimi.selectors.*（候选列表第一位优先），重启服务
```

> 用我们的提取器直接对比输出最省事：`extract_markdown(page, sel)` —— 正文应只含答案，不含「思考已完成…」。
