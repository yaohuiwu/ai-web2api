# Gemini（gemini.google.com）接入说明

> 状态：**默认禁用（`enabled: false`）**，需要时手动开（选择器已校准）。
> 原因：账号侧免费额度限流 —— 实测密集请求后 StreamGenerate 返回 200 且响应头 1.4s 到达，
> 但**流长时间不结束**（300s 超时、页面持续“思考中”），而同一账号在真人浏览器里很快。驱动：`src/ai_web2api/providers/gemini.py`（纯配置驱动）。
> 登录：**需手动登录 Google 账号**（游客态不再可用）；Google 登录不适合自动化（OAuth + 强风控），只能人工完成。

## 1. 实测结论（2026-09-22）

| 项 | 结果 |
|---|---|
| 站点 | `https://gemini.google.com/app`（会话 URL `/app/<hex id>`） |
| 输入框 | `div.ql-editor[contenteditable=true]`（**Quill 编辑器**）→ `type_prompt: true` |
| 发送 | `button[aria-label="发送"]`（英文环境为 `Send message`） |
| **正文容器** | **`.model-response-text`**（count=1、纯正文）；兜底 `message-content .markdown` |
| 用户侧 | `<user-query>`（**不会**被正文选择器匹配 ✓） |
| **登录态** | ✅ 需 Google 账号登录后可用（游客态已不可用）；登录标记见 §3.1 |
| 停止按钮 | ⚠ 生成中未观测到 → `stop_button: []`（靠稳定性判定结束） |
| 思考容器 | ⚠ 未观测到 → `thinking_container: []` |
| 上传入口 | ⚠ 未实测（配置里预留 `input[type=file]`） |

### 为什么登录检测需要**复合选择器**

Gemini 未登录时也有输入框，且顶栏会渲染一个**占位头像**（`default-user`），
裸头像选择器（如 `img.user-icon`）在未登录时也会命中 → 登录态判定必须叠加
「页面上没有登录链接（`a[href*="ServiceLogin"]`）」的约束。详见 §3.1。

## 2. 端到端验证

```
POST /v1/chat/completions {"model":"gemini-web","thread_id":"gemini-cal-1", ...}
→ content='收到'   15.8s   （日志：send: button=button[aria-label="发送"] / done content=2 chars）
文本保真度回归：tests/test_text_fidelity.py -k gemini
```

## 3. 登录（必需）

```bash
./scripts/login.sh gemini     # 手动完成 Google 账号登录 → 自动导入服务
```
> 注：Google 登录不适合自动化（OAuth + 强风控），与 ChatGPT 的情况一致；
> **Gemini 现在必须登录后才能用**，未登录状态无法获得回答。

### 3.1 CLI 登录自动检测（⚠ 未登录占位头像）

`./scripts/login.sh gemini` 用 `login.detect` 自动确认「真的登录成功」（不想等也可回终端按回车）：

```yaml
detect:
  - 'a[href*="SignOutOptions"]'                              # 账号菜单里的「退出登录」
  - 'body:not(:has(a[href*="ServiceLogin"])) img.user-icon'  # 有头像**且**无「登录」入口
```

**坑（实测）**：Gemini 未登录时也有输入框，且顶栏会渲染一个**占位头像**：

```html
<img class="user-icon" alt="个人资料照片" src="https://lh3.googleusercontent.com/a/default-user=s64-c">
```

所以 `img[alt*="个人资料"]` / `img.user-icon` 这类**裸头像选择器在未登录时也会命中**，
会让 CLI 在「还没登录」时就打印「自动检测到登录成功」并保存/导入 `state.json`。
头像必须叠加「页面上没有登录链接（`a[href*="ServiceLogin"]`）」的约束，或只认 `SignOutOptions`。
回归测试：`tests/test_new_providers.py::test_gemini_login_detect_ignores_guest_placeholder_avatar`。

## 3.5 观测型旁听（已启用，用于定位"卡住"）

`network.observe: true`（**不解析 Google 内部协议**，只记状态码与时序）。超时或完成时日志/错误里会带一行：

```
网络旁听：POST .../StreamGenerate status=200 响应头=1.4s 结束=— **流未结束**
```
读法：`status=429` 或响应头很晚 → **排队/限流**；响应头正常但`流未结束` → 站点侧**长思考或流卡住**；
`流已结束` 却无内容 → 提取侧问题。实测（2026-09-22）我们遇到的都是最后一种：**响应头 1.4s 到达、流永不结束**，
且**发完不做任何提取、纯等 120s 也不出结果** → 排除"我们的轮询干扰"，属**账号侧限流**。

## 4. 改版后如何重新校准

```bash
curl -X POST http://127.0.0.1:8000/admin/gemini/debug/probe \
  -H 'Content-Type: application/json' -d '{"message": "只回复两个字：收到"}'
curl 'http://127.0.0.1:8000/admin/gemini/debug/dom?selector=.model-response-text'
curl 'http://127.0.0.1:8000/admin/gemini/debug/dom?selector=user-query'    # 确认用户侧不被误匹配
```
改完 `config.yaml` 只需 `docker compose restart`（配置已挂载，**不必 rebuild**）。
