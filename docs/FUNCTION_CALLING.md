# Function Calling（工具调用）接入设计 + 分步实现计划

> 状态：**已实现**（Step 1–9 均已落地并验证）。
> 参考：`docs/REFERENCE_token-free-gateway.md`（其实现思路）。
> 相关：`docs/DESIGN.md`。

## 1. 目标与范围

**目标**：让 `/v1/chat/completions` 支持 OpenAI 标准的 **Tools / Function Calling**：

- 请求可带 `tools` / `tool_choice`；消息可含 `role=tool`（`tool_call_id`）与 assistant 的 `tool_calls`。
- 响应在需要调用工具时返回标准 `message.tool_calls` + `finish_reason="tool_calls"`（`content=null`）。
- 流式同样支持（SSE `delta.tool_calls`）。
- 客户端零改动：OpenAI SDK 直接把网页版 AI 当原生 function calling 用。

**范围边界**：只做“**文本协议层**”的 FC（prompt 注入 + 输出解析回填），不改驱动。
非文本输出（图片/视频/语音）不在本次范围。

---

## 2. 现状（缺口）

| 点 | 现状 |
|----|------|
| 请求 | `ChatCompletionRequest` 无 `tools` / `tool_choice`（且 `extra="ignore"` 会**静默丢弃**） |
| 消息 | `normalize_message` 明确**忽略** `tool_calls` / `tool_call_id` / `name`；`role=tool` 会被当普通文本 |
| 响应 | `ResponseMessage` 只有 `content`/`reasoning_content`，无 `tool_calls`；`finish_reason` 恒为 `stop` |
| SSE | delta 只有 `content` / `reasoning_content`，无 `tool_calls` |
| Prompt | `build_prompt` 只拼用户文本，没有工具说明/角色转录/工具结果 |

结论：**功能完全缺失**，但接入点清晰（协议层在 `api/`，驱动只收一段文本）。

---

## 3. 设计总览

沿用参考项目的核心思路：**把 tools 注入 prompt，再把输出解析回标准 `tool_calls`**。

```
POST /v1/chat/completions {messages, tools, tool_choice}
        │
        ├─ 需要 FC？ needs = bool(tools) or 最后一条是 role=tool
        │
        ▼  是
  converter.build_prompt(messages, tools, tool_choice)   ← 无状态/create：压平全部历史
  或 converter.build_resume_prompt(messages, tools, tool_choice) ← thread resume：只发本轮
        │        （拼：工具说明 + 角色转录/工具结果 + 示例）
        ▼
  provider.generate(prompt_override=...)   ← 驱动只负责把这串文本发出去
        │
        ▼  网页模型输出文本
  converter.parse_tool_response(text, tools)
        │
        ├─ 命中工具 → 响应 tool_calls（content=null, finish_reason="tool_calls"）
        └─ 否则     → 响应 content（finish_reason="stop"）
        ▼
  客户端本地执行工具 → 把 role=tool 结果再发回来 → 循环
```

**关键原则**：FC 逻辑全部在**协议层（`api/` + 新增 `tool_calling/`）**，驱动保持“文本进、文本出”，
**DeepSeek / Qwen 等驱动零改动**（除了新增一个可选的 `prompt_override` 直发通道）。

---

## 4. 协议/数据模型变更（`api/schemas.py`）

1. 新增类型：
   ```python
   class ToolFunction(BaseModel): name; description: str | None; parameters: dict = {}
   class ToolDefinition(BaseModel): type: Literal["function"]="function"; function: ToolFunction
   ToolChoice = Literal["none","auto","required"] | {"type":"function","function":{"name":...}}
   class ToolCallOut(BaseModel): id: str; type: Literal["function"]="function"; function: {name, arguments: str}
   ```
2. `ChatMessage`：`content` 允许 `str | list | None`；放行 `tool_calls`、`tool_call_id`、`name`（不再忽略）。
3. `ChatCompletionRequest` 增加：`tools: list[ToolDefinition] | None`、`tool_choice: ToolChoice | None`。
   （`tools` 为空 = 完全走旧行为。）
4. `normalize_message`：新增对 `role=tool` / assistant `tool_calls` 的保留（新增字段，不破坏现有 `{role,content}`）。
5. `ResponseMessage` 增加 `tool_calls: list[ToolCallOut] | None = None`；`content` 允许 `None`。
6. `ChatCompletionChoice.finish_reason: Literal["stop","tool_calls","length"]`。
7. SSE：chunk `delta` 增加 `tool_calls`（`index/id/type/function{name,arguments}`）。

> 兼容性：所有新增字段可选，未传 `tools` 时行为与现在**逐字节一致**。

---

## 5. 新增模块 `tool_calling/`

### 5.1 `prompt.py`
- `build_tool_prompt(tools, lang, force_use)`：工具清单 JSON + 输出格式约定 + **一个 few-shot 示例**
  （格式：```tool_json {"tool":"..","parameters":{..}}```）；`force_use` 时加强制语。
- `detect_language(text)`：按中文字符占比选 `cn`/`en`（也可按 `provider.locale` 覆盖）。

### 5.2 `parser.py`
- `extract_tool_calls(text)` / `has_tool_call(text)`：按序尝试
  1. 围栏 ```tool_json```；2. OpenAI `{"tool_calls":[...]}`；3. 裸 `{"tool":..,"parameters":..}`；
  4. XML `<tool_call>`（支持多个）；5. **截断 JSON 自动补右括号**；兼容 `tool`/`name` 两种键名。

### 5.3 `converter.py`
- `resolve_effective_tools(tools, tool_choice)` → `(tools, force_use)`：
  `none` 去工具 / `required` 全量+强制 / `{function:{name}}` 只留该函数+强制 / `auto` 全量。
- `build_prompt(messages, tools, tool_choice)`：**无状态 / create** 用——工具说明 + 角色转录：
  - `system/developer → System:`；`user → Human:`；`assistant(含 tool_calls) → "[Called tools]"+tool_json 块`；
  - `tool → <tool_result tool_call_id="...">...</tool_result>`；最后一条是工具结果 → 追加“据结果回答”。
- `build_resume_prompt(messages, tools, tool_choice)`：**thread resume** 用——页面已有历史，
  只拼「本轮」：可选工具说明（含 defs+格式，保证模型知道格式）+ （若本轮是工具结果）`<tool_result>` + 续写提示；
  否则就是最后一条 user 文本。
- `parse_tool_response(text, tools)` → `(content|None, tool_calls|None, finish_reason)`：
  命中且**名字在本次 tools 内**才算；生成 `call_<uuid24>`；`arguments` 序列化为 JSON 字符串。

---

## 6. 与现有链路的整合

### 6.1 驱动直发通道（唯一驱动改动）
`BaseProvider.generate/complete` 增加可选参数 `prompt_override: str | None = None`；
`WebChatProvider.generate` 里：`prompt = prompt_override or (resume ? last_user_message(...) : build_prompt(...))`。
→ 有 override 时驱动完全不碰消息拼装逻辑，**DeepSeek/Qwen 无需改动**。

### 6.2 路由（`api/routes.py`）
- 计算 `needs_fc = bool(req.tools) or (最后一条消息 role=="tool")`。
- 若 `needs_fc`：按 `mode`（create/resume）选 `build_prompt` / `build_resume_prompt` 作为 `prompt_override`。
- 响应阶段：若 `needs_fc` 且有 `req.tools`，对模型文本调用 `parse_tool_response`：
  - 命中 → 返回 `tool_calls`（`content=null`, `finish_reason="tool_calls"`）；
  - 未命中 → 正常 `content`（`stop`）。
- 非流式与流式都走同一套解析。

### 6.3 流式
- 不带工具：维持现状（逐字 delta）。
- **带工具**：**缓冲正文**（thinking 仍可实时流），生成结束后解析：
  - 命中工具 → 发 `tool_calls` delta（先 `id/name+arguments:""`，再 `arguments`），`finish_reason="tool_calls"`；
  - 否则 → 把缓冲的正文一次性/分块发出，`finish_reason="stop"`。
  - **为什么不能边流边发**：模型原始输出是一段**文本**（工具调用是其中一段 ```` ```tool_json ```` 代码块），
    只有**全文解析完**才知道它是“工具调用”还是“正常回答”。若先把原始文本当 `delta.content` 发出去，
    之后才发现是工具调用，已发出的 SSE 字节**无法撤回**——客户端会既显示一段 JSON 文本、又收到 `tool_calls`，无法使用。
    因此工具请求下流式退化为“先攒后发”；不带工具时仍逐字流，不受影响。

### 6.4 thread 绑定与工具结果
- 我们的 resume 只发最后一条 user 消息；**工具结果回合**要把 `<tool_result>` 作为本轮文本注入
  （这就是 `build_resume_prompt` 的用途）。
- 工具轮次通常每轮都带 `tools`；我们在 resume 时也带上简短工具说明（defs+格式），保证格式可复现。
- model 绑定（thread 固定 model）与 `tools` 每请求可变不冲突。

### 6.5 与 mode / deep_think / search / attachments / options
- 互不影响：`mode` 等仍照旧应用到页面 UI；attachments 仍走文件上传。
- 若带图片且带 tools：图片上传 + prompt 里仍是文本转录（工具结果不含图片）。

---

## 7. 边界与错误处理

| 场景 | 处理 |
|------|------|
| 模型没按格式输出 | `has_tool_call` 为假 → 当普通文本返回（`stop`） |
| 输出里是**不存在的工具名** | 名字校验失败 → 丢弃该 call，当普通文本 |
| JSON 截断/多包一层 | 解析器多格式 + 补括号兜底 |
| `tool_choice=none` | 不加工具说明、不解析（纯文本） |
| `tool_choice=required` 但模型仍没调 | 返回普通文本（不报错；可选：日志 WARNING） |
| 工具结果回合但 `tools` 省略 | 仍注入 `<tool_result>` 并要答案（用 `needs_fc` 覆盖） |
| 死循环（模型反复调工具） | 由客户端控制轮次；网关不限制（可选加“最多 N 轮”提示，暂不做） |

---

## 8. 配置/开关

- 默认**按请求自动启用**（传了 `tools` 才生效）。
- **`server.function_calling: bool = True`**（全局总开关）：
  - `true`：有 `tools` 就走 FC（注入/解析）。
  - `false`：**完全忽略** `tools`（当普通对话处理，不注入工具提示、不解析工具调用）——
    用于**避免注入工具提示触发网页端风控**，或排查问题时一键关掉。
  - 关闭时 `role=tool` 消息也按普通文本处理（不注入 `<tool_result>`）。
- 语言：默认按用户文本自动判断；可用 provider `locale` 起始语言兜底。

---

## 9. 测试计划

- **单元**：`parser`（围栏/裸 JSON/XML/OpenAI 包装/截断修复/纯文本）、
  `converter`（转录格式、tool_choice 三态、命中/未命中、名字校验、`content=null`/`finish_reason`）。
- **schema**：`tools`/`tool_choice`/`role=tool` 解析与放行；未传 tools 时旧行为不变。
- **路由集成**：用 stub provider（返回固定文本，如一个 `tool_json` 块）通过 FastAPI `TestClient`：
  - 非流式：`tool_calls` 形状、`finish_reason`；
  - 流式：SSE `delta.tool_calls` 两段式；
  - 多轮：assistant `tool_calls` + `role=tool` 结果 → 转录文本包含 `<tool_result>`；
  - 未命中：返回普通 `content`。
- **假页 e2e（`slow`）**：fake 页回一个 `tool_json`，验证端到端（可选）。
- **live（`-m live`）**：用 openai SDK 对真实服务发 tools，断言 `tool_calls`（可选）。

---

## 10. 分步实现计划（每步一提交、可验证、不破坏现有行为）

- [x] **Step 1 — schema/类型**：`d3541dc`
- [x] **Step 2 — `tool_calling/parser.py` + 单测**：`c34531e`
- [x] **Step 3 — `tool_calling/prompt.py` + `converter.py` + 单测**：`4d78bd8`
- [x] **Step 4/5 — 路由接入（非流式 + 流式缓冲）**：`cbf1407`
- [x] **Step 6 — thread resume / 工具结果注入**：`e7d4432`
- [x] **Step 7 — 文档（README / DESIGN）**：`5da344d`
- [x] **Step 8/9 — Playground 工具测试 UI + mock 自动执行闭环**：`f29d479`

### 实现位置

| 关注点 | 文件 |
|--------|------|
| 请求/响应 schema（`tools`/`tool_choice`/`tool_calls`/`finish_reason`） | `api/schemas.py` |
| 多格式解析 | `tool_calling/parser.py` |
| 工具说明注入 | `tool_calling/prompt.py` |
| tool_choice / 消息转录 / 响应解析 | `tool_calling/converter.py` |
| 路由编排（`prompt_override`、`tool_calls` 响应、流式） | `api/routes.py`、`providers/base.py`、`providers/webchat.py` |
| 总开关 | `server.function_calling`（`config.py`） |
| 单测 | `tests/test_fc_schema.py`、`test_fc_parser.py`、`test_fc_converter.py`、`test_fc_routes.py` |
| Playground | `webui/playground.html`、`assets/js/playground.js`、`assets/css/playground.css` |

### 实测

- 快速套件 **138 passed**；路由集成测试（stub provider）覆盖非流式/流式/工具结果回合/开关关闭。
- **Playground 闭环 e2e**：工具调用卡 → mock 工具结果 → 最终回答，无 JS 报错。
- **真实 DeepSeek**：`finish_reason=tool_calls`、`content=null`、`tool_calls[0].function.name=get_weather`、`arguments={"city":"东京"}`。

---

## 11. 兼容性与风险

- 无 `tools` 的请求：行为与现在**完全一致**（回归风险低）。
- 主要风险在**流式带工具的缓冲**：需确保 thinking 不受影响、正文不重复/不丢。
- Prompt 注入依赖模型遵守格式 → 用 few-shot + 多格式容错 + 名字校验兜底（非 schema 强约束）。
- 我们比参考项目多一层 thread resume，工具结果回合的注入需专门测试。

---

## 12. 已确认决策

| # | 结论 |
|---|------|
| 1 | 用 OpenAI 原生字段名 `tools` / `tool_choice` ✅ |
| 2 | 流式带工具“先缓冲后发” ✅（原因见 §6.3） |
| 3 | 工具结果回合即使未带 `tools`，仍注入 `<tool_result>` 并要答案 ✅ |
| 4 | 加 `server.function_calling` 全局总开关（默认 `true`；关掉=忽略 tools，用于防/避风控） ✅ |
| 5 | 本次只在 DeepSeek 上验证（Qwen 同源） ✅ |

以上决策均已按方案实现（见 §10）。

---

## 13. Playground 工具调用测试（端到端闭环）

Playground 很适合做 FC 的"端到端自测"：它能扮演 OpenAI 客户端，本地“执行”工具，再回传结果。

**UI（新增“工具”区块）**
- `tools` JSON 文本框（预填示例，如 `get_weather`）；
- `tool_choice` 下拉（auto / required / none）；
- 开关“自动执行 mock 工具” + 内置 mock 注册表（如 `get_weather` 返回固定天气、`calc` 计算）。

**发送**
- 请求体带 `tools`（及 `tool_choice`）；走原有 `/v1/chat/completions`（stream 开/关都支持）。

**渲染**
- 非流式：`message.tool_calls` → 工具调用卡（函数名 + 参数 JSON）；
- 流式：按 `delta.tool_calls[].index` 累积 `name`/`arguments`，结束后渲染同样的卡。

**mock 自动执行闭环（核心）**
1. 收到 `tool_calls` → 本地对每个 call 调用 mock 工具得到结果；
2. 追加 `assistant(tool_calls)` 与 `tool(tool_call_id, result)` 到消息历史；
3. 自动再发一次请求（仍带 `tools`）→ 显示最终回答；
4. 循环上限（如 5 轮）防死循环；每步都在聊天区以“工具调用 / 工具结果 / 最终回答”卡片展示。

**消息历史改造**
- 现在 playground 只保存 user 文本；需升级为**保存完整 `messages`**（含 `assistant.tool_calls` 与 `tool` 结果），才能支持多轮工具往返。
- 与 `thread_id` 共存：带工具也走 thread；工具结果回合的注入由后端 `build_resume_prompt` 处理。

**验收**
- 不传 tools：行为不变（逐字流）；
- 传 tools 问“东京天气”：看到工具调用卡 → mock 结果 → 最终自然语言回答；
- 流式与非流式都能显示 `tool_calls`。
