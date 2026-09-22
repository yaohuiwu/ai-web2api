# Kimi（kimi.com）接入说明（脚手架，待校准）

> 状态：**默认 `enabled: false`**。已实测：登录方式、输入框、发送按钮、附件入口。
> **待校准**：`response_container`、`login_check`、`stop_button`（需登录后实测，本文末给了步骤）。
> 驱动：`src/ai_web2api/providers/kimi.py`（`KimiProvider`，纯配置驱动，复用通用 `WebChatProvider`）。

## 1. 实测结论（2026-09，未登录状态）

| 项 | 结果 | 说明 |
|---|---|---|
| 站点 | `https://www.kimi.com/` | `kimi.moonshot.cn` / `kimi.com` 均 302 到它 |
| 登录方式 | **微信扫码 / 手机号 + 验证码** | 弹层里有 `phone-form__phone-input`(tel) / `phone-form__code-input`(验证码)，且带**易盾验证码** `yidun_input` → 无密码登录，**自动化不现实**，故 `login.mode: manual` |
| 输入框 | `div.chat-input-editor[contenteditable=true]` | 无 `textarea`；逐字输入 `press_sequentially` **有效**（受控组件受理），故 `type_prompt: true` |
| 发送按钮 | `div.send-button-container` | 输入内容后才出现 |
| 附件入口 | `input.hidden-input[type=file][multiple]` | 隐藏 input，直接 `set_input_files` |
| 消息列表 | `.message-list` / `.message-list-container` | 容器已确认；**单条正文容器未确认** |
| 游客可用 | 未登录**也能在输入框输入** | → 若不校准 `login_check`，会把「未登录」误判成「已登录」 |

## 2. 校准步骤（约 5 分钟）

```bash
# 1) 打开配置
#    config.yaml → providers.kimi.enabled: true
# 2) 手动登录（在宿主执行，会打开浏览器）
./scripts/login.sh kimi          # 或 ai-web2api login kimi
# 3) 发一条测试消息，让服务 dump 响应区 DOM（这是现有的调试接口）
curl -X POST http://127.0.0.1:8000/admin/kimi/debug/probe \
  -H 'Content-Type: application/json' -d '{"message": "你好"}'
#    → 从输出里挑出「每个助手回复只有一个」的容器，填进 selectors.response_container 第一位
# 4) 逐个验证其它选择器（返回数 > 0 即命中）
curl 'http://127.0.0.1:8000/admin/kimi/debug/dom?selector=.message-list'
curl 'http://127.0.0.1:8000/admin/kimi/debug/dom?selector=%5Bclass*%3D%22stop%22%5D'
# 5) 登录态判定：登录前后各跑一次，挑「只在登录后出现」的选择器
curl 'http://127.0.0.1:8000/admin/kimi/debug/dom?selector=.user-area__main'
# 6) 把确认的选择器写进 config.yaml（候选列表第一位优先匹配），重启服务
```

## 3. 已知取舍

- **不逆向内部 API**：本项目原则是纯 DOM 自动化，Kimi 同样走 `selectors`；若后续 UI 改版，只改配置。
- `selectors.stop_button` 留空也能跑：结束判定退化为「稳定 N 次无变化」（`stable_polls`），只是慢一点。
- `server.function_calling` 对 Kimi 同样适用（prompt 注入，与站点无关）。
- 若 Kimi 出现「同一会话串行排队」类问题，调 `thread_busy_timeout`（默认 20s）。
