# 参考：Token-Free Gateway 的优势与可借鉴点

> 对象项目：<https://github.com/andeya/token-free-gateway>（本地代码：`~/Downloads/token-free-gateway-main`）
> 本文只做**学习笔记**，不改 ai-web2api 代码。

## 0. 它是什么

一个 **OpenAI 兼容网关**：把 14 个网页版 AI（ChatGPT/Claude/Gemini/DeepSeek/Kimi/Qwen/GLM/Grok/…）
变成统一的 `/v1/chat/completions`，**纯浏览器自动化、无需 API key**，并且支持
**Full Tools / Function Calling**。

技术栈：Bun + TypeScript；浏览器用 **Playwright（core）通过 CDP 连接用户本机正在运行的 Chrome**。

分层：

```
server.ts / openai/chat-completions.ts     ← HTTP + OpenAI 协议 + tool 编排
        tool-calling/{prompt,parser,converter}.ts   ← tools 注入/解析
        providers/<name>/{auth,client,stream,index}.ts   ← 各站点实现
        providers/factory/{base-api-client,base-dom-client}.ts  ← 两类通用基类
        providers/shared/{page-lifecycle,cookie-parser,error-guard,stream-helpers}.ts
        browser/manager.ts (CDP 单例) + cli/{chrome,webauth,daemon}.ts
```

---

## 1. 最值得学：Prompt 注入式 Function Calling

网页端只有“一个文本框”，没有原生 tools。它的做法是**把 tools 教进 prompt，再把输出解析回标准 tool_calls**。
（详见 `src/tool-calling/`）

**流程**

```
tools + messages ──buildPromptFromMessages──► 单段 prompt（工具说明 + 角色转录 + 示例）
        │
        ▼  网页模型输出文本
   文本 ──parseToolResponse──► { content:null, tool_calls:[...], finish_reason:"tool_calls" }
        │
        ▼  客户端本地执行工具
   role=tool 结果 ──► 格式化成 <tool_result tool_call_id=...> 再喂回网页模型 ──► 最终答案
```

**关键设计（都值得学）**

1. **注入结构清晰**（`prompt.ts`）：工具清单 = `{name, description, parameters}` 的 JSON；附**一个 few-shot 示例**
   规定输出格式 ```tool_json {"tool":..,"parameters":..}```；支持中英自动切换；`tool_choice=required`
   时加“必须调用工具，不要直接文字回答”的强制提示。
2. **多格式容错解析**（`parser.ts`）：按序尝试「围栏 ```tool_json」→「OpenAI 风格 `{"tool_calls":[...]}`」→
   「裸 `{"tool":..,"parameters":..}`」→「XML `<tool_call>`」→「**截断 JSON 自动补右括号**」；
   兼容 `tool`/`name` 两种键名。→ 降低“模型不守格式”的失败率。
3. **名字校验**（`converter.ts`）：解析出的函数名必须出现在本次请求的 `tools` 里，否则**退回当普通文本**。
   → 防止模型“幻觉调用”不存在的工具。
4. **严格对齐 OpenAI 语义**：产出 tool_calls 时 `content=null`、`finish_reason="tool_calls"`；
   `tool_calls[].id` 生成 `call_<uuid24>`；`arguments` 序列化为 JSON 字符串。
5. **多轮压缩成文本**：`system→System:`、`user→Human:`、`assistant(含 tool_calls)→"[Called tools]"+tool_json 块`、
   `tool→<tool_result tool_call_id=...>`；若最后一条是工具结果，追加“请根据工具结果回答”。
6. **tool_choice 三态+指定函数**：`none` 去工具 / `required` 加强制语 / `{function:{name}}` 只留该函数并强制 /`auto` 全量。
7. **流式**：不带工具时逐字 delta；**带工具时先缓冲全文→解析→再按规范发 tool_call delta**
   （先发 `id/name+arguments:""`，再发 `arguments`）。工具场景接受“先攒后发”。
8. **pre-stream 错误**（`chat-completions.ts`）在建立 SSE **之前**就返回真正的 HTTP 状态码
   （鉴权/限流/模型不可用），而不是塞进 SSE 里让客户端解析不出来。

> 取舍：这是“让模型自觉输出规定格式”的方案，**不是 schema 强约束**；需要示例 + 容错 + 校验兜底。

---

## 2. 浏览器层：CDP 连接“真实、已登录的 Chrome”

`browser/manager.ts` 是个**单例**，用 `chromium.connectOverCDP(wsUrl)` 连接用户本机
`--remote-debugging-port` 的 Chrome：

- **并发合并**：`getContext()` 用 `connecting` promise 合并并发连接（只建一次）。
- **自动重连**：监听 `browser.on("disconnected")`，下次请求自动重连；`isHealthy()` 做轻量探活。
- **自动拉起**：连接失败时 `tryAutoStartChrome()`（调 `cli/chrome.ts`）。
- **页面复用**：`getPage(domain)` 先找 URL 含该域名的已存在 tab，避免重复开页。
- **优雅关闭**：`shutdown()` 只断开 CDP，不杀用户的 Chrome。

为什么值得注意：**在真实浏览器里发请求**天然带着用户的登录态/指纹，能**绕过 Cloudflare/风控**；
代价是需要本机常驻 Chrome，**Docker/无头部署不友好**。

（对比：ai-web2api 是 Playwright 启动独立 Chromium + `storage_state`，部署简单，但更容易被风控。）

---

## 3. Provider 抽象：两类基类 + 共享 helper

`providers/factory/` 把站点分两类：

- `BaseApiClient`：直接 `page.evaluate(fetch(...))` 打站点内部 API（带 cookie）。
- `BaseDomClient`：纯 DOM 交互（找输入框→粘贴→回车→轮询）。
  - 提供 `pollForStableText(extractFn)`：**轮询到稳定**的通用实现（间隔/最大等待/稳定阈值可配）。
  - 子类只实现 `sendViaDom()` / `parseStreamImpl()` / `getCookies()`。

`providers/shared/`：

- `page-lifecycle.ensurePage()`：页面活着就复用，死了自动重建并注入 cookie。
- `cookie-parser.ts`：把 cookie 字符串/文件解析成 CDP 可注入的结构。
- `error-guard.throwIfSessionExpired()`：401/403 → 统一 `SessionExpiredError`。
- `stream-helpers.textToStream()`：把缓冲文本包成 `ReadableStream`（DOM 型 provider 复用同一套流接口）。

> 这正是我们 `WebChatProvider` 的思路，但它把“**API 型 / DOM 型**”显式分成两个基类，
> 并把“轮询到稳定 / 页面复用 / 会话过期”沉淀成共享 helper。

---

## 4. 协议与错误处理

- `openai/types.ts`：完整实现 OpenAI 的 `tools/tool_choice/tool_calls/tool_call_id`、
  `content: string|null`、`finish_reason: stop|tool_calls|length`、SSE `ToolCallDelta` 等。
- 错误映射（`chat-completions.ts`）：`SessionExpiredError→401`（并**驱逐缓存的 provider 客户端**）、
  `ProviderApiError→原样透传 provider 的 4xx`、其它→`502`；统一 `{error:{message,type}}`。
- `usage` 用 `ceil(len/4)` 估算，保证字段完整（客户端兼容）。

---

## 5. 工程化（可选择性借鉴）

- **CLI**：`serve/start/stop/chrome/webauth/daemon` 子命令 + 守护进程。
- **多平台分发**：编译成独立二进制，按 `darwin/linux/win` 架构做 npm 包（`packaging/npm/*`）。
- **测试**：`test/tool-parser.test.ts`、`test/tool-converter.test.ts` 对解析/转换有**针对性单测**
  （含截断 JSON、多 XML、OpenAI 包装等边界）。
- `error-guard` 这类“一致性小工具”避免每个 provider 重复判断。

---

## 6. 对照 ai-web2api：建议借鉴清单（按优先级）

| 优先级 | 借鉴点 | 说明 |
|--------|--------|------|
| **高** | **Prompt 注入式 Function Calling** | 我们目前**完全没有** FC：`normalize_message` 会忽略 `tool_calls`，请求 schema 无 `tools`。可把 `prompt/parser/converter` 三段逻辑直接移植成 Python（见下）。 |
| 中 | **名字校验 + 多格式容错解析** | 移植时务必带上；否则模型乱输出会误触发工具。 |
| 中 | **错误语义分层** | 我们已有部分（NotLoggedIn/ThreadExpired 等）；可补一个统一的“会话过期→401 + 失效标记”。 |
| 中 | **轮询到稳定参数化** | 我们已有 `stable_polls/min_wait_before_stable/poll_interval`；可再抽成显式“稳定判定”配置。 |
| 低 | **CDP 复用真实 Chrome** | 能缓解风控，但 Docker 化困难；**暂不建议**，除非将来做“本机模式”。 |
| 低 | **CLI 守护进程 / 单二进制分发** | 我们用 Docker Compose，收益有限。 |

### 若做 Function Calling，我们需要的改动（预览）

1. `api/schemas.py`：请求加 `tools`、`tool_choice`；消息支持 `role=tool`（`tool_call_id`）；
   响应 `ResponseMessage` 加 `tool_calls`、`finish_reason` 支持 `tool_calls`；SSE delta 加 `tool_calls`。
2. 新增 `tool_calling/{prompt,parser,converter}.py`：注入 + 解析 + 消息压平 + `tool_choice` 语义。
3. `build_prompt` / `last_user_message`：把「工具说明 + 角色转录（含 `<tool_result>`）」拼进 prompt；
   **thread resume 时**把 `<tool_result>` 当作新的 user 文本注入（我们只发最后一条 user，这点它与我们不同）。
4. 流式：复用“缓冲正文→解析→发 tool_calls”的思路（与它一致）。

---

## 7. 它仓库里的关键文件索引

| 关注点 | 文件 |
|--------|------|
| FC 主流程 | `src/openai/chat-completions.ts` |
| 工具注入 | `src/tool-calling/prompt.ts` |
| 输出解析 | `src/tool-calling/parser.ts` |
| 消息↔prompt / 响应解析 / tool_choice | `src/tool-calling/converter.ts` |
| OpenAI 类型 | `src/openai/types.ts` |
| FC 单测 | `test/tool-parser.test.ts`、`test/tool-converter.test.ts` |
| 浏览器 CDP 单例 | `src/browser/manager.ts` |
| Provider 基类 | `src/providers/factory/base-api-client.ts`、`base-dom-client.ts` |
| 共享 helper | `src/providers/shared/{page-lifecycle,cookie-parser,error-guard,stream-helpers}.ts` |

---

## 8. 一句话结论

最值得学的是 **① Prompt 注入 + 多格式容错解析 + 严格 OpenAI 语义 的 Function Calling**，
其次是把 **“浏览器生命周期 / 页面复用 / 轮询稳定 / 会话过期”沉淀成共享 helper** 的工程手法。
CDP 连真实 Chrome 很强但与我们 Docker 化的路线相冲，**不建议照搬**。
