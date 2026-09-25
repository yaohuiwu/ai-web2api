# Claude（claude.ai）接入说明

> 状态：**已实测**（登录态 + 容器经宿主代理）。驱动：`src/ai_web2api/providers/claude.py`。
> 登录：**仅手动**（Google / 邮箱验证码，无密码自动登录）。

## 1. 实测结论（2026-09-25）

| 项 | 值 |
|---|---|
| 站点 | `https://claude.ai/new`（会话页 `/chat/<uuid>`） |
| 输入框 | `div.ProseMirror[contenteditable=true]` → `type_prompt: true` |
| 发送 | `button[aria-label="Send message"]`（`locale: en-US` 固定英文 UI） |
| 停止 | `button[aria-label*="Stop"]`（生成中出现、结束消失） |
| **正文容器** | **`.font-claude-response`**（干净正文）；`[data-testid="assistant-message"]` 的 innerText 会混入无障碍文案 `Claude responded: …`，**不要直接用它** |
| 完成信号 | `button[data-testid="action-bar-copy"]`（该条消息工具栏出现 == 写完）→ `done_toolbar` |
| **网络流** | `POST /api/organizations/<org>/chat_conversations/<id>/completion`（`fetch` + SSE） |
| SSE 事件 | `event: content_block_delta` / `data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"…"}}`；思考为 `thinking_delta`（`delta.thinking`） |
| 登录判定 | `login_check: []`：未登录访问 `/new` 会跳 `/login`（无 composer）→ 用输入框判定 |
| 会话 URL | `/chat/<uuid>` → `session_url_pattern = r"/chat/([0-9a-fA-F-]{8,})"` |
| 登录 cookie | `.claude.ai sessionKey`（实测有效期约 28 天） |

抓取策略：**网络为主**（`network.url_pattern` + `_parse_sse_snapshot`），DOM 兜底
（`.font-claude-response`）。`stream_content: false` + `preview_stream: true`（重渲染多）。

## 2. 网络抓取与 fetch 增量

Claude 用 `fetch` + 流式 body。通用抓取脚本已改为**不阻塞页面**：立即把 response 交给
页面，另开 `resp.clone().body.getReader()` 增量追加到 `window.__aiw2a_sse`（原来
`await clone().text()` 会阻塞流式 UI 且只能等流结束才拿到内容）。

两个 Claude 专属适配：
- **结束标记**：Claude 发完 `message_stop` 后**不关连接**（继续推 ping）→ 覆写
  `_net_stream_complete()` 用 `message_stop` 判结束；
- **给网络通道优先时间**：发送后 Claude 会立刻渲染一个空助手壳，而 SSE 首帧要 ~1s
  才到 → `NET_FIRST_GRACE = 3.0`，避免过早因 "DOM moved before SSE" 降级。

## 3. 登录（手动，一次即可）

```bash
./scripts/login.sh claude      # 弹出浏览器：Google / 邮箱验证码登录 → 自动导入 state
curl http://127.0.0.1:8000/admin/claude/login/status
```

> **地区限制**：容器内浏览器无法直达 `claude.ai`（跳 `claude.com/app-unavailable-in-region`）。
> 解法：在 `.env` 设 `WEB2API_PROXY=http://host.docker.internal:7890`（宿主代理）；
> Playwright 按 **context** 应用代理 → 国内 provider 在 `config.yaml` 用 `proxy: false` 直连。

## 4. 改版后如何重新校准

```bash
curl -X POST http://127.0.0.1:8000/admin/claude/debug/probe \
  -H 'Content-Type: application/json' -d '{"message": "只回复两个字：收到"}'
curl 'http://127.0.0.1:8000/admin/claude/debug/dom?selector=.font-claude-response'
curl 'http://127.0.0.1:8000/admin/claude/debug/dom?selector=%5Bdata-testid%3D%22assistant-message%22%5D'
```
改完 `config.yaml` 只需 `docker compose restart`（配置已挂载，**不必 rebuild**）。

## 5. 待实测项

- 扩展思考折叠区（DOM）→ `thinking_container`（SSE 已能解析 `thinking_delta`，DOM 侧留空）
- 模型下拉（多模型切换）→ `model_menu`（当前单模型 `claude-web`）
- 附件入口 → `upload_input`
