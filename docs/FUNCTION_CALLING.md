# Function Calling（工具调用）接入设计 + 分步实现计划

> 状态：**设计，待 review/批准**。批准后再改代码。
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
  - （与参考项目一致：工具场景流式退化为“先攒后发”，避免已发出的正文无法撤回。）

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

- 默认**按请求自动启用**（传了 `tools` 才生效），无需配置。
- 可选加 `server.function_calling: bool = True`（全局关闭用），先不加，保持简单。
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

- **Step 1 — schema/类型**：请求 `tools`/`tool_choice`、`role=tool`、响应 `tool_calls`/`finish_reason`、SSE delta；
  `normalize_message` 保留工具字段。加 schema 单测。（提交）
- **Step 2 — `tool_calling/parser.py`**（移植 + 单测）。（提交）
- **Step 3 — `tool_calling/prompt.py` + `converter.py`**（移植 + 单测）。（提交）
- **Step 4 — 非流式路由接入**：`prompt_override` 直发通道；`needs_fc` 判定；响应解析为 `tool_calls`。（提交）
- **Step 5 — 流式路由接入**：带工具时缓冲正文 → 发 `tool_calls` delta。（提交）
- **Step 6 — thread resume / 工具结果注入**（`build_resume_prompt`）。（提交）
- **Step 7 — 集成测试（stub provider / 假页）+ 文档更新（README、DESIGN）**。（提交）

每步都跑：快速套件（默认）；Step 4–7 视情况跑一次 `-m slow`。

---

## 11. 兼容性与风险

- 无 `tools` 的请求：行为与现在**完全一致**（回归风险低）。
- 主要风险在**流式带工具的缓冲**：需确保 thinking 不受影响、正文不重复/不丢。
- Prompt 注入依赖模型遵守格式 → 用 few-shot + 多格式容错 + 名字校验兜底（非 schema 强约束）。
- 我们比参考项目多一层 thread resume，工具结果回合的注入需专门测试。

---

## 12. 需要确认的点

1. **API 面**：`tools` 用 OpenAI 原生字段名（`tools` / `tool_choice`），对吗？
2. **流式带工具**接受“先缓冲后发”（不是逐字流），对吗？（与参考项目一致）
3. 工具结果回合若客户端**没带 tools**，仍注入 `<tool_result>` 并要答案——可以吗？
4. 是否需要 `server.function_calling` 全局开关？（我倾向先不加）
5. 本次是否**只在 DeepSeek 上验证**即可（Qwen 同源，天然支持）？
