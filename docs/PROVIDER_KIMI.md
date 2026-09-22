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

## 2. 落盘（token 轮换后不丢）

`BrowserManager` 的「登录态变化即落盘」指纹**同时包含 cookies 与 localStorage**
（`_state_fingerprint`）——Kimi/DeepSeek 的 token 就在 localStorage，只比较 cookies 会漏，
导致重启/导入时用的是**过期快照**。指纹变化即写 `state.json`（后台每 `login_check_interval` 检查一次）。

## 3. 已知取舍

- **正文为缓冲发送**（`stream_content: false`）：Kimi 把思考与正文放在同一个 segment，逐字 diff 会把「思考已完成…」当正文发出去；缓冲后一次发更稳（代价：没有逐字流式观感）。
- **无停止按钮**：结束判定靠 `stable_polls`（当前 20）× 3 + `min_wait_before_stable`，短回答约 20s；想更快可调小 `stable_polls`（有截断风险）。
- **认证有效期已可显示**：Kimi 登录态在 **localStorage**（cookie 里没有），故配了
  `login.auth_local_storage: ["refresh_token"]` —— 直接解该 JWT 的 `exp`（实测 **约 90 天**），
  UI 显示「还剩 N 天」，并且因为是 `mode: manual`，**快过期会提醒**更新登录态。
- **`access_token` 只有几分钟不用管**：Kimi 前端在调用接口时自动用 `refresh_token` 换新的
  （我们驱动的是真实页面；实测发消息前它自己就刷新了）。我们要做的只是**把轮换后的登录态落盘** ——
  见下方「落盘」一条。
- 不逆向内部 API：与其它 provider 一样只走 DOM 选择器，改版只需改配置。

## 4. 改版后如何重新校准（约 5 分钟）

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
