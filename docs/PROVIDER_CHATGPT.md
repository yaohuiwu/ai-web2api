# ChatGPT Provider 接入设计（chatgpt.com）

> 状态：**设计，待评审**。批准后再改代码。
> 相关：`docs/DESIGN.md`（§3 Provider 扩展点）、`docs/MANUAL_LOGIN.md`（手动登录）、`docs/PROVIDER_QWEN.md`（同类站点经验）。

## 1. 目标与范围

把 **chatgpt.com** 作为新 provider 接入（`gpt-*` / `o*` 系列模型），走同一套 `/v1/chat/completions`。
本次只做**文本对话/流式/思考（尽力）**；附件、联网、Canvas 等尽量支持但可后续校准。

**明确的困难**（与 DeepSeek/Qwen 最大不同）：
1. **没有密码自动登录**：OpenAI 用邮箱验证码 / Google / Apple / Microsoft OAuth / Passkey。
2. **强 Cloudflare 人机验证**：headless 自动化很可能被拦（"Verify you are human" / 403）。

## 2. 登录方案（核心）

`login.mode` 只能 **manual**。用我们已经做好的两件基础设施：

- **推荐：CLI 手动登录**（宿主有头浏览器，能过验证码/Google/Cloudflare）：
  ```bash
  python -m ai_web2api.cli login chatgpt        # 登录成功后自动导入本地服务
  ```
  登录后 `storage_state`（含 `cf_clearance`、session cookie）落盘；headless 服务复用。
- **兜底：`/ui` 导入**：粘贴 / 上传 `state.json` → `POST /admin/chatgpt/login/state`。

**Cloudflare 注意**：
- `cf_clearance` 与 **IP + User-Agent** 绑定。登录与后续请求必须在**同一出口 IP + 同一 UA**（本项目已固定 UA，Docker 与宿主同 IP 出口通常满足）。
- `cf_clearance` 有效期有限、且可能随会话刷新；过期表现为请求被拦 → 重新手动登录一次即可。
- 若 headless 仍被挑战，可退路（二选一，二期再议）：
  a) 服务临时 `CHATGPT_HEADLESS=false` 走有头；
  b) 参考 token-free-gateway：**CDP 连本机真实 Chrome**（改造较大，非本次范围）。

## 3. Provider 配置骨架（选择器待登录后实测校准）

先给"候选值"，真正落地时用 `/admin/chatgpt/debug/dom` + 探针校准（与 Qwen 相同流程）。

```yaml
providers:
  chatgpt:
    driver: chatgpt
    url: https://chatgpt.com/
    locale: en-US                 # ChatGPT 默认英文 UI，选择器用英文 aria-label
    session_url: "{base}/c/{id}"  # 会话 URL: chatgpt.com/c/<uuid>
    models:
      # 对外名 = 原模型名 + -web（与 deepseek-web 一致）
      - {name: gpt-5-web,     ui_label: "GPT-5"}
      - {name: gpt-4o-web,    ui_label: "GPT-4o"}
      - {name: o3-web,        ui_label: "o3"}
    selectors:
      input:
        - "#prompt-textarea"                                   # contenteditable（ProseMirror）
        - "div.ProseMirror[contenteditable='true']"
      send_button:
        - 'button[data-testid="send-button"]'
        - 'button[aria-label="Send prompt"]'
      stop_button:                                             # 生成中用停止按钮判定结束（可靠）
        - 'button[data-testid="stop-button"]'
      response_container:                                      # 助手消息里的 markdown 正文
        - '[data-message-author-role="assistant"] .markdown'
      thinking_container: []                                   # o 系列"思考"结构待实测
      login_check:                                             # 登录后才有的账号入口
        - '[data-testid="profile-button"]'
        - 'button[aria-label*="Account"]'
      new_chat_button:
        - 'a[data-testid="create-new-chat-button"]'
        - '[aria-label="New chat"]'
      model_menu:                                              # 顶部模型切换下拉
        trigger: ['button[data-testid="model-switcher-dropdown-button"]']
        option:  ['[role="menuitemradio"]:has-text("{label}")', '[role="option"]:has-text("{label}")']
      attachment_menu:                                         # 附件：先"+"再上传，或直接 file input
        trigger: ['button[aria-label*="Add"], button[data-testid="composer-plus-btn"]']
        item:    ['[role="menuitem"]:has-text("Add photos & files")', '[role="menuitem"]:has-text("Upload")']
        file_input: ['input[type="file"]']
      # 联网/深度研究等工具（若在"+"菜单里）用 toggle_button 或后续扩展
    login:
      mode: manual
      hint: "ChatGPT 无密码登录，请用 CLI/UI 手动登录后导入 state"
```

> 选择器是**候选**，必须登录后逐项校准；尤其 `input` 是 contenteditable、`thinking_container` 未知。

## 4. 需要的代码改动（预计很小）

目标是**复用 `WebChatProvider`，驱动零/极少改动**：

1. **注册驱动**：`providers/registry.py` 的 `DRIVERS` 加 `"chatgpt": ChatGPTProvider`；
   新增 `providers/chatgpt.py`：`class ChatGPTProvider(WebChatProvider)`，只需登记 `session_url_pattern`。
   （可选）把 `session_url_pattern` 提到 `ProviderConfig`（配置化），则连子类都可省。
2. **`_send_prompt` 支持"逐字输入"**：ChatGPT 输入是 `contenteditable`（ProseMirror/React）。
   引擎现在用 `fill()`；Qwen 的经验是 React 受控输入 `fill()` 可能不生效。
   → 给引擎加**配置项** `selectors.type_prompt: true`（用 `press_sequentially` 输入，再点发送）。
   （或直接对 contenteditable 一律用 type。）
3. **`network`（可选，二期）**：ChatGPT 流式走 `/backend-api/conversation`（SSE JSON patch）。
   一期先走 **DOM 兜底**；二期再写 `parse_stream` 钩子提速/更稳。
4. **无需改**：登录（manual 已有 CLI/UI）、thread 绑定/历史、附件框架、菜单框架、options 透传。

## 5. 模型命名与别名冲突

- 对外模型名 = **原模型名 + `-web`**：`gpt-5-web` / `gpt-4o-web` / `o3-web`…
- 我们内置了 `OPENAI_COMMON_MODELS` 兜底别名（`gpt-4o`、`o3-mini`…），**与 ChatGPT 的原生名高度重合**：
  - 别名（如 `gpt-4o`）→ 由 `server.default_provider` 决定挂给谁；
  - 模型名（`gpt-4o-web`）与别名（`gpt-4o`）不冲突。
- 若把 ChatGPT 设为 `server.default_provider`，则 `gpt-4*` 等别名会指向 ChatGPT 默认模型——由你决定。

## 6. 与 token-free-gateway 的对照

- 它用 **CDP 连本机真实 Chrome**，天然带真实指纹与登录态，**绕 Cloudflare 最稳**。
- 我们的路线是 **Playwright 独立 Chromium + storage_state**，部署简单（Docker），但 CF 更严时可能被拦。
- 因此本期先按"手动登录拿 state（含 cf_clearance）→ headless 复用"验证；若 CF 反复拦，再评估 CDP 方案（单独立项）。

## 7. 分步计划（批准后）

- **Step 1** — 引擎加 `type_prompt` 选项（contenteditable 用逐字输入）+ 单测。（通用能力，DeepSeek/Qwen 不受影响）
- **Step 2** — 新增 `providers/chatgpt.py` + 注册（`session_url_pattern=/c/<uuid>`），配置骨架（选择器占位）。
- **Step 3** — 你手动登录一次（CLI 或 UI 导入 state），我据此**校准选择器**（`debug/dom` + 探针）：
  input / send / stop / response / login_check / new_chat / model_menu / attachment_menu。
- **Step 4** — 端到端验证：`/v1/models` 出现 `gpt-*-web`；非流式/流式一轮对话；`thread_id` 续用；模型切换；附件。
- **Step 5** — 文档（README provider 列表、PROVIDER_CHATGPT 实测结果）。

## 8. 需要你确认

1. **模型对外名**用 `gpt-5-web` / `gpt-4o-web` / `o3-web` 这种"原名+`-web`"，对吗？（沿用既定约定）
2. 是否把 **ChatGPT 设为 `server.default_provider`**（`gpt-4o` 等别名指向它）？还是保持 DeepSeek 为默认？
3. `type_prompt` 选项：做成**配置项**（`selectors.type_prompt: true`）还是对 contenteditable 自动判断？（我倾向配置项，明确可控）
4. 校准需要你**先手动登录一次**并导入 state（ChatGPT 无法自动登录）——可否？
5. 若 headless 被 Cloudflare 拦，本期是否接受"用 CLI 重新登录刷新 cf_clearance"作为唯一手段（暂不做 CDP）？
