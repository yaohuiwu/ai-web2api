# 元宝（yuanbao.tencent.com）接入说明

> 状态：**已启用**（选择器已按登录态实测校准）。
> 驱动：`src/ai_web2api/providers/yuanbao.py`（纯配置驱动）。
> 登录：**必须登录**（微信扫码 / 手机 / QQ），只能手动；无密码自动登录。

## 1. 实测结论（2026-09，登录态）

| 项 | 结果 |
|---|---|
| 站点 | `https://yuanbao.tencent.com/chat/{agentId}/{chatId}`（默认 agent `naQivTmsDa`） |
| 输入框 | `div.ql-editor[contenteditable=true]`（**Quill**，`type_prompt: true`） |
| 发送 | `#yuanbao-send-btn`（**DIV**，`aria-label="发送"`；不是 button） |
| **正文容器** | `.hyc-content-md .hyc-common-markdown`（**流式渐进** 23→421 字，可 diff） |
| **思考容器** | `.hyc-component-deep-search-agent__think-container` |
| 停止按钮 | `[aria-label*="停止"]`（生成中出现、结束消失） |
| 模型 | `/api/agent/model/list` → `hunyuan_gpt_175B_0404`(Hunyuan) / `deep_seek_v3`(DeepSeek) |
| 流式端点 | `POST /api/chat/<conversationId>`（`text/event-stream`，仅观测不解析） |
| 代理 | `proxy: false`（国内站点直连） |

> ⚠ 页面**没有可见的模型选择器**（未见 Hunyuan/DeepSeek 文案），故只对外暴露
> `yuanbao-web`（默认 Hunyuan）。将来若校准出 `model_menu` 再加第二个模型。

## 2. 登录（必填，手动）

```bash
./scripts/login.sh yuanbao     # 微信扫码 / 手机 / QQ → 自动导入服务
```

**登录标记（关键）**：

| 状态 | 页面标记 |
|---|---|
| 已登录 | 右上角头像 `.yb-common-nav__ft__avatar`（内含 `<img>`） |
| 未登录 | 右上角「登录」按钮 `button.agent-dialogue__tool__login`；左下角「未登录」 |

```yaml
login_check:                      # 登录后**才出现**
  - ".yb-common-nav__ft__avatar img"
  - ".yb-common-nav__ft__avatar"
logged_out:                       # 可见即未登录（反向保险）
  - "button.agent-dialogue__tool__login"
  - "text=未登录"
login:
  mode: manual
  detect: [".yb-common-nav__ft__avatar img"]
```

**踩过的坑**：
1. **不能用 input 当登录标记**：未登录也有 `div.ql-editor`（placeholder「请登录后输入内容」）。
2. **不能用宽泛 `[class*="avatar"]`**：未登录时会命中骨架 `.yb-nav__user-skeleton__avatar`
   → 服务误判「已登录」。必须用 `.yb-common-nav__ft__avatar`。
3. 若登录态已导入但 UI 显示「未登录」，多半是 `login_check` 选择器不匹配
   （`check_login` 返回 False）；改完 `config.yaml` 需 `docker compose restart`。

## 3. 思考区 vs 正文区（关键陷阱）

DOM 层级（登录态实测）：

```
.agent-chat__list__item--ai
  .agent-chat__bubble--ai
    .agent-chat__conv--ai__speech_show
      .hyc-component-deep-search-agent          ← 会**包住正文**！不能当思考容器
        .hyc-component-deep-search-agent__think__header-container
          .hyc-component-deep-search-agent__think-container   ← 真正的思考文本
        .hyc-content-md.hyc-content-md-done
          .hyc-common-markdown                  ← 正文（答案）
```

如果把 `.hyc-component-deep-search-agent` 配成 `thinking_container`，引擎会把
**正文当成思考**输出（实测：content 为空、thinking 里是答案）。正确写法：

```yaml
response_container: [".hyc-content-md .hyc-common-markdown",
                     ".agent-chat__list__item--ai .hyc-common-markdown"]
thinking_container: [".hyc-component-deep-search-agent__think-container",
                     ".hyc-component-deep-search-agent__think__header-container"]
```

回归测试：`tests/test_new_providers.py::test_yuanbao_provider_registered_and_configured`
（断言思考容器不是整个 `deep-search-agent`）。

## 4. 端到端验证

```bash
curl -s -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"yuanbao-web","messages":[{"role":"user","content":"只回复两个字：收到"}],"stream":false}'
# → {"content":"收到", ...}
```

## 5. 改版后如何重新校准

```bash
# 1) 看正文/思考容器
curl -X POST http://127.0.0.1:8000/admin/yuanbao/debug/probe \
  -H 'Content-Type: application/json' -d '{"message": "只回复两个字：收到"}'
curl 'http://127.0.0.1:8000/admin/yuanbao/debug/dom?selector=.hyc-common-markdown'
curl 'http://127.0.0.1:8000/admin/yuanbao/debug/dom?selector=.hyc-component-deep-search-agent__think-container'
# 2) 改 config.yaml 后：docker compose restart（配置已挂载，不必 rebuild）
```
