# 豆包（doubao.com）接入说明（脚手架，待校准）

> 状态：**默认 `enabled: false`**。驱动：`src/ai_web2api/providers/doubao.py`（`DoubaoProvider`，纯配置驱动）。
> 登录：**仅手动**（无密码登录：手机号验证码/扫码）。

## 1. 实测结论（2026-09）

| 项 | 结果 |
|---|---|
| 站点 | `https://www.doubao.com/chat` |
| 输入框 | `div.tiptap.ProseMirror[contenteditable=true]`（**tiptap/ProseMirror，无 textarea**）→ `type_prompt: true` |
| 输入框占位 | `p[data-placeholder="发消息..."]` |
| 消息容器（游客页 DOM 可见） | `.message-container` / `.chat-item` / `.chat-main-messages` —— 登录后结构待确认 |
| 登录入口 | 右上角 `.login-button`（游客页） |
| **区域限制** | **容器内**打开直接提示：「受区域限制，请先登录再使用豆包。你也可以选择使用 Dola。」→ **未登录拿不到输入框**，无法用游客态校准 |
| 发送按钮 | 输入内容后才出现（游客页被区域限制挡住，未取到）→ 待校准 |
| 会话 URL | 形如 `https://www.doubao.com/chat/<数字 id>`（已配 `session_url_pattern = r"/chat/(\d{6,})"`，待确认） |

## 2. 校准步骤（登录后 ~5 分钟）

```bash
# 1) 手动登录（宿主执行；豆包无密码登录）
./scripts/login.sh doubao          # 或 ai-web2api login doubao
# 2) config.yaml → providers.doubao.enabled: true 并重启服务
# 3) 发一条测试消息，dump 响应区 DOM（现有调试接口）
curl -X POST http://127.0.0.1:8000/admin/doubao/debug/probe \
  -H 'Content-Type: application/json' -d '{"message": "只回复两个字：收到"}'
# 4) 逐个确认候选（返回数 > 0 即命中）
curl 'http://127.0.0.1:8000/admin/doubao/debug/dom?selector=.message-container'
curl 'http://127.0.0.1:8000/admin/doubao/debug/dom?selector=button%5Baria-label%3D%22%E5%8F%91%E9%80%81%22%5D'
# 5) 把确认的选择器填进 config.yaml 的 selectors.{send_button,response_container,thinking_container,login_check}
```

- `login_check`：豆包**未登录时不渲染输入框**（区域限制），因此可以先留空（框架用 input 判定）；
  登录后若发现游客态也能进，请改成"登录后才出现"的元素（否则会把未登录误判为已登录）。
- `send_button`：若找不到按钮，可留空 → 框架用 **Enter 发送**（`type_prompt: true` 时逐字输入）。
- `thinking_container`：豆包有"深度思考"折叠区，若正文里出现思考文案，按 `docs/PROVIDER_KIMI.md` 的做法
  收窄选择器（**先窄后宽**），必要时把正文选择器改成"按内容剔除思考"。

## 3. 已知取舍

- 纯 DOM 自动化，不逆向内部接口；改版只需改配置。
- 未登录时容器内受区域限制，**必须**先手动登录；登录态落盘（`profiles/doubao/state.json`）后重启自动恢复。
