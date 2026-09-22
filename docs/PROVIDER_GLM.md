# 智谱清言（chatglm.cn）接入说明

> 状态：**可用，但需要"人工过 WAF 一次 + 保活"**。驱动：`src/ai_web2api/providers/glm.py`。
> 登录：**仅手动**（无密码登录：手机号验证码/扫码）。

## 1. 站点前置：阿里云 WAF 滑动验证

| 环境 | 结果 |
|---|---|
| 宿主 headless | ❌ 「滑动验证页面：请按住滑块，拖动到最右边」 |
| 容器 headful（Xvfb） | ❌ 同样被拦（`editable=0`） |
| 容器 headful + **服务同款反自动化参数 + 真实 UA** | ❌ 仍被拦（09:21:21 UTC 实测） |

**结论：换参数/换 headless 都过不去；能过的是"人"。** 但流程可行：

```bash
./scripts/login.sh glm     # 打开真实可见窗口：先手动拖滑块过 WAF，再完成登录
```
实测（2026-09-22）：宿主完成一次人工验证后，`storage_state` 里同时带上了
**WAF cookie**（`acw_sc__v3` / `acw_tc` / `cdn_sec_tc` / `ssxmod_itna*`）与
**登录 cookie**（`chatglm_token` / `chatglm_refresh_token` / `chatglm_user_id`），
导入服务后**容器内可直接复用**（无需再过滑块，标题为「智谱清言」并显示用户名）。

> ⚠️ **保活**：WAF 票据有存活期（阿里云通常分钟~小时级，且与 IP/指纹相关）。失效后需要**再人工过一次**。
> 因此 GLM 适合"临时用一下"，不适合长期无人值守；长期稳定建议改用**智谱开放平台官方 API**。

## 2. 实测选择器（已校准）

| 项 | 值 |
|---|---|
| 输入框 | `textarea.scroll-display-none`（**可见 textarea**，不是 contenteditable → 用 `fill()`） |
| 发送 | **无发送按钮** → `send_button: []`（回车发送） |
| **正文容器** | `.markdown-body:not(.thinking-content *)`（count=1，纯正文） |
| 思考容器 | `.thinking-content .markdown-body` |
| 为什么必须排除 | **思考与正文都用 `.markdown-body`**；直接取 `.answer-content .markdown-body` 会拿到 2 个（末条是正文，但拼接时会把思考混进来） |
| 登录判定 | `.sidebar-user-name` / `.userInfoBar`（登录后侧栏显示用户名） |
| 缓冲/拼接 | `stream_content: false` + `response_all_new: true`（表格/代码重渲染多、回答可能拆多容器） |
| 会话 URL | 形态未确认（`alltoolsdetail?…`）→ 暂不启用 thread 恢复 |

## 3. 端到端验证

```
GET /admin/glm/login/status        → logged_in: true
POST /v1/chat/completions {"model":"glm-web","thread_id":"glm-cal-1", ...}
→ content='我是智谱清言，由智谱AI开发的大语言模型GLM驱动的智能助手，可以帮你回答问题、写作创作、分析问题等。'   19.7s，无思考污染
文本保真度回归：tests/test_text_fidelity.py -k glm
```

## 4. 改版后如何重新校准

```bash
curl -X POST http://127.0.0.1:8000/admin/glm/debug/probe \
  -H 'Content-Type: application/json' -d '{"message": "只回复两个字：收到"}'
curl 'http://127.0.0.1:8000/admin/glm/debug/dom?selector=.markdown-body'   # 看正文/思考的区分是否还在
```
改完 `config.yaml` 只需 `docker compose restart`（配置已 bind mount，不必 rebuild）。
