# 多 Provider 扩展设计与 Qwen 接入方案（待评审）

> 状态：**已实现**（通用化重构 + Qwen 接入均已落地，见「10. 实现结果与实测校准」）。
> 相关：`docs/DESIGN.md`（§3 Provider 扩展点）、`docs/HISTORY.md`（会话历史）

---

## 0. 结论速览

- 现状：**“加 provider” 这条路已经通了**（`DRIVERS` 注册 + 配置化选择器 + 基类抽象），
  DeepSeek 之外再挂一个 provider 的骨架是有的。
- 但**通用引擎被困在 `DeepSeekProvider` 里**，且有几处 DeepSeek 假设写在了通用层，
  直接照抄写 Qwen 会大量复制粘贴、并踩到硬编码。
- 建议先做一次**“把通用能力上提到基类 + 选项系统扩展”**的重构（DeepSeek 行为不变），
  再落 Qwen 驱动。这样 Qwen 驱动只需实现少量钩子，能力还能做得更全。
- Qwen 关键差异：**登录页独立**（`/auth`）、**模型是下拉菜单**、**模式是下拉菜单（自动/思考/快速）**、
  附件是 `input#filesUpload`——当前框架只支持 radio/toggle，缺 **menu/dropdown** 能力。

---

## 1. 现状：扩展机制盘点

| 层 | 机制 | 位置 |
|----|------|------|
| 配置 | `ProviderConfig`：`name/driver/url/models/selectors/login/queue/...` | `config.py` |
| 注册 | `DRIVERS = {"deepseek": DeepSeekProvider}`，`driver` 字段或 `name` 选类 | `providers/registry.py` |
| 驱动 | `BaseProvider`（登录/凭据/开页/`complete` 聚合）+ 子类实现 `generate()` | `providers/base.py` |
| 选择器 | `SelectorsConfig`：input / send_button / response_container / thinking_container / stop_button / login_check / new_chat_button / mode_button / mode_checked / toggle_button / toggle_checked / upload_input | `config.py` |
| 登录 | `LoginConfig`：mode(auto/manual/cookies)、`username_env/password_env`、登录页选择器 | `config.py` + `base.auto_login` |
| 会话 | `ThreadManager`（thread_id 绑定/持久化/恢复）、`session_url_pattern` | `core/threads.py` |
| API | `mode` / `deep_think` / `search` / `attachments` / `thread_id`（`extra_body`） | `api/schemas.py`、`api/routes.py` |
| 历史 | SQLite `threads` / `messages` | `core/store.py` |

**能直接复用的**：多 provider 注册、每 provider 独立 Context/登录态/队列、thread 绑定与历史、
SSE 聚合、错误映射、`/v1/*` OpenAI 兼容面。

---

## 2. 扩展性评估：问题清单

按影响从大到小：

- **P1 · 通用引擎写在驱动里**。`generate()` 的整套流程（开页 → 应用选项 → 上传附件 → 发送 →
  网络优先/DOM 兜底轮询 → diff 增量 → 忙/失效/超时判定）全在 `DeepSeekProvider`。
  新 provider 要么复制 800 行，要么先重构。**这是最大的扩展性障碍**。
- **P2 · 会话恢复 URL 硬编码**。`core/threads.py:267` 写死
  `f"{cfg.url}/a/chat/s/{id}"`——Qwen 路径不是 `/a/chat/s/`。通用层混入了 DeepSeek 细节。
- **P3 · 没有“在 UI 里选模型”的能力**。`ModelConfig.ui_label` **定义了但从未被使用**；
  `generate(model=...)` 的 `model` 参数在 DeepSeek 驱动里**完全没用**。Qwen 的模型是头部下拉，
  必须在页面点选，否则模型名只是路由标签、切不了模型。
- **P4 · 选项只有 radio / toggle 两种形态**。`mode_button`（常驻 radio）和 `toggle_button`（开关）。
  Qwen 的**模型**和**模式**都是 **Ant Design 下拉菜单**（`span.ant-dropdown-trigger` /
  `.qwen-thinking-selector`），需要“点开 trigger → 点 option → 读回校验”的 **menu** 形态。
- **P5 · 网络抓取写死**。`_NET_INIT_JS` 里 URL 正则 `/api/v0/chat/completion`、全局量名
  `__aiw2a_sse`、以及 `_parse_sse_snapshot`（DeepSeek 的 JSON-Patch 协议）都是 DeepSeek 专属。
- **P6 · 思考提取写死**。`_THINK_PANEL_JS` 依赖“已思考（用时 N 秒）”中文标题；Qwen 的思考区结构不同。
- **P7 · resume 不允许切 mode**。`_apply_options(can_set_mode=not resume)` 假设“会话页没有模式区”。
  Qwen 的模式位于输入框内，**resume 时也应在**，应可配置。
- **P8 · 附件上传写死**。`_upload_attachments` 直接 `set_input_files` + 等 `img[src^=blob:]` 预览。
  Qwen 有 `input#filesUpload`（可直接用），但“上传入口/打开菜单/等待完成”应做成钩子。
- **P9 · 登录页 URL 只能复用 `cfg.url`**。`auto_login` 走 `goto(cfg.url)`；Qwen 登录在独立的
  `/auth`，需要 `login.url` 覆盖。
- **P10 · locale 是全局的**。`browser.locale` 单值；DeepSeek 要 zh-CN，Qwen 也可能按语言渲染文案。
  多 provider 各自语言应可覆盖。
- **P11 · API 选项字段 DeepSeek 化**。请求只有 `mode/deep_think/search`；且 `ChatCompletionRequest`
  的 `extra="ignore"` 会**静默丢弃**未知字段，provider 特有选项（如 Qwen 的额外能力）无处传。
- **P12 · 多 provider 的路由/别名**。`_model_map` 以模型名为 key，**跨 provider 同名会冲突**；
  `OPENAI_COMMON_MODELS` 兜底别名**只挂给第一个启用的 provider**（`break`），双 provider 时语义含糊。
- **P13 · `ProviderConfig` 没有扩展位**。Pydantic 默认忽略未知字段，provider 特有配置没有正规入口。

---

## 3. 设计目标与原则

1. **通用框架保持通用**：新能力全部做成**可选、配置驱动**；不开启时行为与现在一致（DeepSeek 零回归）。
2. **驱动只写差异**：把通用流程提到基类，驱动实现少量钩子（解析、选项映射、特殊 UI 动作）。
3. **能力最大化**：模型选择、模式（单选/下拉）、开关、附件、思考、流式、会话恢复、别名。
4. **向后兼容**：现有 `config.yaml`/`.env`/API 不改也能跑；新增字段全部可选。

---

## 4. 目标架构

### 4.1 分层

```
BaseProvider                      # 登录/凭据/开页/complete 聚合（保持）
   └── WebChatProvider            # 新增：通用聊天引擎（从 DeepSeek 抽出）
          ├── DeepSeekProvider    # 只留 DeepSeek 差异（SSE 解析/思考面板/网络 JS/预设）
          └── QwenProvider        # 只留 Qwen 差异
```

`WebChatProvider.generate()` 复用现有流程，把差异点变成钩子：

| 钩子 | 默认 | DeepSeek 覆写 | Qwen 覆写 |
|------|------|---------------|-----------|
| `init_scripts()` | 由 `network.url_pattern` 生成 XHR 抓取 JS（可关） | 返回现有 `_NET_INIT_JS` | 待实测端点后填 |
| `parse_stream(snapshot) -> (thinking, content)` | 原样返回（无网络解析则走 DOM） | 现有 `_parse_sse_snapshot` | 待实测；先空 → DOM |
| `extract_thinking(page, sel, idx)` | 取元素 `innerText` | 现有 `_THINK_PANEL_JS` | 默认 |
| `apply_options(...)` | 通用（radio/toggle/menu），见 4.2 | 加 `MODE_PRESETS` 翻译 | 用 `mode_menu`/`model_menu` |
| `session_url(url_id)` | `cfg.session_url` 模板 | `/a/chat/s/{id}` | `/c/{id}`（待实测） |
| `attachment_preview_selector()` | `img[src^='blob:']` | 默认 | 默认 |
| `before_upload(page)` | 由 `attachment_menu.trigger` 决定是否点开 | 无 | 无（有 `#filesUpload`） |

### 4.2 选项系统（声明式，三种形态）

`SelectorsConfig` 新增可选子结构，**在保留现有字段基础上叠加**：

```yaml
selectors:
  # ---- 已有，保持不变 ----
  input: [...]
  send_button: []
  response_container: [...]
  thinking_container: [...]
  stop_button: []
  login_check: []
  new_chat_button: [...]
  mode_button: {}          # 形态 A：常驻 radio（旧版 DeepSeek）
  mode_checked: [...]
  toggle_button: {...}      # 形态 B：开关
  toggle_checked: [...]
  upload_input: [...]

  # ---- 新增（全部可选）----
  model_menu:               # 形态 C：下拉菜单 —— 选模型
    trigger: ["span.ant-dropdown-trigger"]
    option:  ['div[role=option]:has-text("{label}")']  # {label} ← model.ui_label
    current: [".header-left"]                          # 可选：读回校验
  mode_menu:                # 形态 C：下拉菜单 —— 选模式
    trigger: [".qwen-thinking-selector .qwen-chat-v2-dropdown-menu-trigger"]
    option:  ['div[role=option]:has-text("{label}")']
    current: [".qwen-thinking-selector .qwen-chat-v2-dropdown-menu-select-label"]
    labels: {auto: "自动", thinking: "思考", fast: "快速"}  # API mode → UI 文案
  attachment_menu:          # 附件
    trigger: []             # 空 = 直接 set_input_files（Qwen 用 #filesUpload）
    file_input: ["input#filesUpload"]
```

- 三种形态统一在 `apply_options()` 里执行，执行后**读回校验**（复用现有 `_set_toggle` 的“校验+重试+告警”思路）。
- `can_set_model` / `can_set_mode` 由 provider 策略决定（默认：新会话可设，resume 可配置）。

### 4.3 网络抓取框架（可选）

新增 `network`（provider 级，可选）：

```yaml
network:
  capture: true
  url_pattern: "/api/v0/chat/completion"     # 生成 XHR 监听 JS
  grace_seconds: 12                           # 宽限期无事件 → DOM 兜底
```

- 抓取 JS 与 `parse_stream` 钩子分离：JS 通用（按 `url_pattern` 匹配、把 responseText 存全局），
  解析由 provider 实现。
- **不配 `network` → 直接走 DOM 轮询**（现有兜底路径已经够用）。

### 4.4 API 选项透传（可选）

`ChatCompletionRequest` 增加：

```python
options: dict[str, Any] = Field(default_factory=dict)  # provider 自定义选项，透传给驱动
```

- 保留 `mode/deep_think/search`（DeepSeek 兼容）；
- `options` 让 Qwen 特有选项（例如将来的 `web_search`、`image_gen`）无需改 schema；
- `extra="allow"`（或继续 `ignore` + 显式 `options`）——倾向**显式 `options`**，避免乱传。

### 4.5 其它通用化

- `ProviderConfig.locale`（可选）→ 覆盖 `browser.locale`；Context 按 provider 创建，天然隔离。
- `LoginConfig.url`（可选）→ 自动登录用的登录页（Qwen `/auth`）。
- `ProviderConfig.session_url`（可选模板 `{base}/{id}`）→ 替代硬编码 `/a/chat/s/`。
- `ModelConfig.ui_label` 终于有了用途：`model_menu.option` 的 `{label}`。
- 多 provider：注册时对**重复模型名告警**；`OPENAI_COMMON_MODELS` 兜底别名改为
  “给 `server.default_provider`（或首个启用 provider）”。

---

## 5. Qwen 接入设计（基于实测探针）

### 5.1 实测到的 DOM（2026-09，`zh-CN`，未登录首页）

| 目标 | 实测 |
|------|------|
| 登录页 | `https://chat.qwen.ai/auth`，标题 “Qwen Studio” |
| 邮箱框 | `input[name=email]`，placeholder「输入你的电子邮箱」 |
| 密码框 | `input[name=password]`，placeholder「输入你的密码」 |
| 登录按钮 | `button` 文案「登录」（另有 Google / Github 按钮） |
| 聊天输入框 | `textarea.message-input-textarea`，placeholder「询问 Qwen」 |
| 模型下拉 | `.header-left span.ant-dropdown-trigger`（文案 `Qwen3.7-Plus`）；点开后 `[role=option]` |
| 可选模型（示例） | `Qwen3.7-Plus`、`Qwen3.8-Max`、`Qwen3.8-Omni-Flash` …（以线上为准，配置化） |
| 模式下拉 | `.qwen-thinking-selector` 的 `.qwen-chat-v2-dropdown-menu-trigger` / `...-select-label`（文案「自动」） |
| 模式取值 | 自动 / 思考 / 快速（→ `auto` / `thinking` / `fast`） |
| 附件 | `input[type=file]#filesUpload`（aria「上传文件」） |

> 登录后聊天页的**回复容器/思考容器/send 按钮/会话 URL 形态**需登录态再实测校准。

### 5.2 Qwen 配置草案（`config.yaml` 片段）

```yaml
providers:
  qwen:
    driver: qwen
    url: https://chat.qwen.ai/
    locale: zh-CN
    session_url: "{base}/c/{id}"        # 待实测确认
    models:
      - {name: qwen-max,     ui_label: "Qwen3.8-Max"}
      - {name: qwen-plus,    ui_label: "Qwen3.7-Plus"}
      - {name: qwen-omni,    ui_label: "Qwen3.8-Omni-Flash"}
    model_aliases:
      qwen-turbo: qwen-plus
    selectors:
      input: ["textarea.message-input-textarea"]
      send_button: []                   # 待实测（无则回车发送）
      response_container: []            # 待实测
      thinking_container: []            # 待实测
      login_check: ["textarea.message-input-textarea"]
      new_chat_button: []               # 待实测
      model_menu:
        trigger: [".header-left span.ant-dropdown-trigger"]
        option:  ['div[role=option]:has-text("{label}")']
      mode_menu:
        trigger: [".qwen-thinking-selector .qwen-chat-v2-dropdown-menu-trigger"]
        option:  ['div[role=option]:has-text("{label}")']
        current: [".qwen-thinking-selector .qwen-chat-v2-dropdown-menu-select-label"]
        labels: {auto: "自动", thinking: "思考", fast: "快速"}
      attachment_menu:
        file_input: ["input#filesUpload"]
    login:
      mode: auto
      url: https://chat.qwen.ai/auth
      username_env: QWEN_USERNAME
      password_env: QWEN_PASSWORD
      page:
        username: ["input[name=email]"]
        password: ["input[name=password]"]
        submit: ['button:has-text("登录")']
```

### 5.3 能力清单（目标）

- [x] 文本对话 + 流式（SSE 聚合）
- [x] 思考内容（`reasoning_content`）
- [x] 模型选择（下拉）——经 `model` 名 → `ui_label` 点选
- [x] 模式：auto / thinking / fast（下拉）
- [ ] 附件：图片/文件（`#filesUpload`，先做图片）
- [ ] 联网搜索等（若 Qwen 在 UI 提供开关，用 `toggle_button` 接入）
- [x] 会话绑定/历史/重启恢复
- [ ] 别名（`model_aliases`）

“尽可能多”= 模型、模式、附件、思考、联网（若可配置）、会话恢复、别名；**图片生成/视频/语音等非文本输出暂不做**
（OpenAI 兼容面是 chat completion，输出多模态需要另设计）。

---

## 6. 兼容性与风险

- 所有新增配置字段**可选**；不配时走旧路径，DeepSeek 行为应逐字节不变。
- 重构 `generate()` 到基类是主要风险面 → 用现有测试兜底：
  `test_mode_presets`（假页 e2e）、`test_history_endpoint`、`test_toggle_verify`、
  `test_openai_compat`（live），并新增 Qwen 的假页/单测。
- 网络抓取依赖前端实现，易随改版失效 → 始终保留 DOM 兜底（现状已有）。
- Qwen 登录风控未知，沿用“半自动”策略：优先自动登录，失败提示 cookies/手动。
- 多 provider 同名模型/别名冲突 → 注册期告警 + 唯一命名约定。

---

## 7. 实施计划（分步、可独立提交、每步可验证）

> 原则：**先通用化，再落 Qwen**；每步都不破坏 DeepSeek。

- **Step A（重构，无新功能）**：抽出 `WebChatProvider`，把 `generate()`/轮询/选项应用/附件/send
  通用化；`DeepSeekProvider` 只保留差异钩子。跑全部现有测试，行为不变。
- **Step B（选项系统）**：`SelectorsConfig` 增加 `model_menu` / `mode_menu` / `attachment_menu`；
  实现 menu 形态的“点开→点选→读回校验”；接上 `ui_label`。加单测（假页）。
- **Step C（通用化小件）**：`ProviderConfig.locale`、`LoginConfig.url`、`ProviderConfig.session_url`
  模板（去掉 `/a/chat/s/` 硬编码）、`session_url()` 钩子。
- **Step D（附件框架）**：`attachment_menu`（trigger/preview 钩子），保留 DeepSeek 现有行为。
- **Step E（网络抓取框架）**：`network.url_pattern` 生成通用 XHR JS + `parse_stream` 钩子；
  DeepSeek 迁移到该框架（或先并存）。
- **Step F（API 扩展）**：`options: dict` 透传 + 请求 schema 文档。
- **Step G（Qwen 驱动落地）**：新增 `QwenProvider` + `config.yaml` 段；**登录一次实测**校准
  回复/思考/send/会话 URL/网络端点；能用网络抓取就上，否则 DOM 兜底。
- **Step H（多 provider 收尾）**：重复模型名告警、默认 provider 别名归属、README/示例、
  live 测试标 `live`。

每步产出：代码 + 对应测试 + 文档更新，一步一提交。

---

## 8. 需要你确认的开放问题

1. **是否同时启用 DeepSeek + Qwen？** 若是，`gpt-4*` 等兜底别名归谁？（建议：新增
   `server.default_provider`，默认第一个启用者；其余 provider 用显式 `model_aliases`。）
2. **Qwen 模型清单**：`ui_label` 取线上当前可选项（如 `Qwen3.8-Max` / `Qwen3.7-Plus` /
   `Qwen3.8-Omni-Flash`）；`models[].name` 你希望用什么对外名（`qwen-max` / `qwen-plus` / …）？
3. **模式命名**：API 传 `mode=thinking|fast|auto`（推荐，provider 各自映射）可以吗？
4. **能力边界**：本次只做「文本+思考+模型+模式+图片附件+（可选）联网」，
   **不做**图片生成/视频/语音输出，可以吗？
5. **登录联调**：Step G 需要你提供 Qwen 密码（或先跑一次有头手动登录、我再导出 `state.json`）。
6. **Step A 重构范围**：允许我改动 `DeepSeekProvider` 的内部结构（保持对外行为与配置不变）吗？

---

## 10. 实现结果与实测校准

Step A–H 已全部完成（每步一提交，快速套件 95 passed + 假页 e2e 6 passed）：

| 步骤 | 提交 | 内容 |
|------|------|------|
| A | `refactor(providers): extract generic WebChatProvider...` | 通用引擎上提，DeepSeek 只留差异 |
| B | `feat(providers): declarative model/mode dropdown menus...` | 下拉菜单选项 + `ui_label` 模型选择 |
| C | `feat(providers): per-provider locale, login.url, session_url...` | provider 级 locale/登录页/会话 URL 模板 |
| D | `feat(providers): attachment menu framework...` | 附件框架 |
| E | `feat(providers): config-driven XHR/SSE capture...` | 网络抓取框架 |
| F | `feat(api): generic options passthrough...` | `options` 透传 |
| G | `feat(qwen): register Qwen provider... / calibrate selectors...` | Qwen 驱动 + 实测校准 |
| H | `feat(registry): server.default_provider alias ownership...` | 多 provider 别名归属 |

### 登录后实测校准（2026-09，`chat.qwen.ai`）

| 项 | 实测值 |
|----|--------|
| 登录页 | `https://chat.qwen.ai/auth`，`input[name=email]` / `input[name=password]` / `button:has-text("登录")` |
| 聊天输入 | `textarea.message-input-textarea`（placeholder「询问 Qwen」） |
| **发送按钮** | 输入后才渲染：`button.send-button` / `.chat-prompt-send-button`（圆形向上箭头） |
| 正文容器 | `.response-message-content.phase-answer`（`.phase-answer` 区分思考/回答） |
| 思考容器 | `.qwen-chat-thinking-status-card-content`（内容为「已经完成思考」等状态，非思考全文） |
| 模型下拉 | `span.ant-dropdown-trigger` → `div[role=option]:has-text("{label}")`（如 `Qwen3.7-Plus` / `Qwen3.8-Max`） |
| 模式下拉 | `.qwen-thinking-selector ...` → 选项 `自动/思考/快速` |
| 附件 | `input#filesUpload` |
| **会话 URL** | `https://chat.qwen.ai/c/<uuid>` → `session_url: "{base}/c/{id}"` |
| 流式端点 | 观察到 `POST /api/v2/chat/completions`（暂未接入，先走 DOM 兜底） |
| 登录检测 | `text=新建对话`（登录后才有） |

实测结果：`model=qwen3.7-plus-web` → `content="2"`、`reasoning="已经完成思考"`；
`mode=thinking` → 日志 `mode -> '思考'` 且正常回复。

### 待办 / 注意

- Qwen 偶发 `net::ERR_CONNECTION_CLOSED`（疑似风控/限流），重试即可。
- 思考全文（非状态）需进一步校准（可能需展开折叠面板）。
- 流式端点 `/api/v2/chat/completions` 的 SSE 格式待接入 `network` 解析（可选）。

---

## 11. 登录失败根因与修复（2026-09 调研）

Qwen 登录/保持登录高频失败，逐个定位到 **4 个可修复点 + 1 个外部因素**：

| # | 现象 | 根因 | 修复 |
|---|------|------|------|
| 1 | 输入账号密码后点「登录」**零请求** | 登录框是 **React 受控输入**：`fill()` 只改 DOM 值、React state 仍为空 → 提交被静默跳过 | 改用**逐字输入** `press_sequentially`（`base.auto_login`） |
| 2 | 手动登录：第一次点「登录」无效、**第二次才行** | 网页首发提交偶发静默 no-op | 自动登录**重试** `login.retries`（默认 3） |
| 3 | 页面卡在 `#splash-screen`（`#root` 空、body 空）→ 误判未登录 | Qwen 限流时 SPA 不 boot | `_goto_ready`：卡加载屏时 **reload 重试** + 更长等待（`check_login`/`open_chat_page`/`auto_login`） |
| 4 | 默认是「使用验证码登录」tab | 需先点「使用密码登录」 | `login.page.password_tab` + 点后等密码框、没出来再点一次 |
| 5 | `auth.qwen.ai/api/v2/auths/refresh` 返回“令牌已撤销，请重新登录” | Qwen 服务端撤销会话 | 后台检测**防抖**（连续 2 次失败才判掉线）+ 掉线**自愈重登** |

**外部因素（无法完全消除）**：同一账号/IP 短时间大量自动化访问会触发 Qwen 风控——表现为“页面不 boot / 返回未登录页 / 令牌被撤销”。
缓解手段：降低 `browser.login_check_interval`（已从 300s 调到 900s）、让账号冷却、必要时用**真实浏览器手动登录一次**并导入 `state.json`/cookies。
