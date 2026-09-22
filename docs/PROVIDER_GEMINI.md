# Gemini（gemini.google.com）接入说明

> 状态：**默认禁用（`enabled: false`）**，需要时手动开（选择器已校准）。
> 原因：账号侧免费额度限流 —— 实测密集请求后 StreamGenerate 返回 200 且响应头 1.4s 到达，
> 但**流长时间不结束**（300s 超时、页面持续“思考中”），而同一账号在真人浏览器里很快。驱动：`src/ai_web2api/providers/gemini.py`（纯配置驱动）。
> 登录：**游客态即可用**（实测无需登录、无验证码）；Google 账号登录为**可选**、且只能手动。

## 1. 实测结论（2026-09-22）

| 项 | 结果 |
|---|---|
| 站点 | `https://gemini.google.com/app`（会话 URL `/app/<hex id>`） |
| 输入框 | `div.ql-editor[contenteditable=true]`（**Quill 编辑器**）→ `type_prompt: true` |
| 发送 | `button[aria-label="发送"]`（英文环境为 `Send message`） |
| **正文容器** | **`.model-response-text`**（count=1、纯正文）；兜底 `message-content .markdown` |
| 用户侧 | `<user-query>`（**不会**被正文选择器匹配 ✓） |
| **游客态** | ✅ 可用：直接发消息就能得到回答（实测"只回复两个字：收到" → `收到`，13.7s） |
| 当前模型 | 游客态显示 **Flash-Lite** |
| 停止按钮 | ⚠ 生成中未观测到 → `stop_button: []`（靠稳定性判定结束） |
| 思考容器 | ⚠ 游客态未观测到 → `thinking_container: []` |
| 上传入口 | ⚠ 未实测（配置里预留 `input[type=file]`） |

### 关于"额度用尽 → 自动降级 Flash-Lite 仍可免费聊天"

那是**站点行为**，对我们**无需特殊处理**：我们只读 DOM，抓取逻辑与模型无关；
实测游客态当前就是 Flash-Lite，回答同样能完整抓到。若将来降级时出现"提示横幅/弹窗"，
按下面的方式配 `dismiss_button` / `busy_hint` 即可（见 `docs/PROVIDER_KIMI.md` 的做法）。

### 为什么**不配** `logged_out`（与豆包相反）

豆包是"游客态有输入框但**发不出去**" → 必须靠反向标记判未登录；
Gemini 是"游客态**可以正常对话**" → 游客是**合法可用状态**，
因此 `login_check: []`（用输入框判定"可用"）、且**故意不配** `logged_out`，否则会把可用状态拒掉。
若站点改成"游客不可发消息"，再补 `logged_out: ['button:has-text("登录")']`。

## 2. 端到端验证

```
POST /v1/chat/completions {"model":"gemini-web","thread_id":"gemini-cal-1", ...}
→ content='收到'   15.8s   （日志：send: button=button[aria-label="发送"] / done content=2 chars）
文本保真度回归：tests/test_text_fidelity.py -k gemini
```

## 3. 可选：登录（保存历史 / 更多能力）

```bash
./scripts/login.sh gemini     # 手动完成 Google 账号登录 → 自动导入服务（非必需）
```
> 注：Google 登录不适合自动化（OAuth + 强风控），与 ChatGPT 的情况一致；**但 Gemini 不登录也能用**，
> 所以本 provider 默认就能工作，不必先登录。

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
