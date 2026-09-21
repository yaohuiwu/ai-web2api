# ai-web2api 设计文档

> 把「网页版 AI 对话」包装成 OpenAI 兼容 API 的服务。
> 当前示例实现：DeepSeek 网页版（chat.deepseek.com），通过 Playwright 驱动浏览器完成 打开网页 → 登录 → 输入消息 → 获取响应。

---

## 1. 目标与边界

### 1.1 目标
- 提供一个 **OpenAI 兼容** 的 HTTP API（`/v1/chat/completions`、`/v1/models`），支持流式与非流式。
- 通过 **Provider 模式** 抽象不同网页 AI 的差异，新增一个网页 AI 只需实现一个 Provider 类。
- 以 DeepSeek 网页版为第一个参考实现。

### 1.2 非目标 / 边界
- 不做多轮上下文管理（网页 UI 本质是「单次输入 → 单次输出」，`messages` 中只取最后一条 user 消息 + system 前缀，见 §5.3）。
- 不做账号风控对抗（不做验证码识别、不绕过限流）。
- 不保证与官方 API 完全等价：`usage` 等字段为估算值。

---

## 2. 总体架构

```
┌─────────────────────────────────────────────────────┐
│  客户端 (OpenAI SDK / curl / 任意 OpenAI 兼容调用方)   │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP (SSE 流式 / JSON)
┌──────────────────────▼──────────────────────────────┐
│  FastAPI Server  (main.py / api/routes.py)           │
│   /v1/models  /v1/chat/completions  /healthz        │
│   Auth: Bearer 校验 (可选)                           │
└──────────────────────┬──────────────────────────────┘
                       │ Provider 抽象
┌──────────────────────▼──────────────────────────────┐
│  BaseProvider (providers/base.py)                   │
│  chat() / chat_stream() / login() / close()         │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────┐  │
│  │ DeepSeekWeb  │  │ (未来)      │  │  (未来)    │  │
│  │  (Playwright)│  │  (本地模拟)   │  │  Kimi/…   │  │
│  └──────┬───────┘  └──────────────┘  └───────────┘  │
└─────────┼───────────────────────────────────────────┘
          │ Playwright (async)
┌─────────▼───────────────────────────────────────────┐
│  BrowserManager (browser/manager.py)                │
│  持久化上下文 + storage_state 登录态复用 + 请求串行锁  │
└─────────────────────────────────────────────────────┘
```

### 分层职责

| 模块 | 职责 |
|------|------|
| `main.py` | FastAPI 入口：路由挂载、可选 API key 鉴权、后台登录态刷新、优雅关停 |
| `api/schemas.py` | OpenAI 兼容请求/响应模型（含多部分 content → 附件提取） |
| `api/routes.py` | `/v1/*` OpenAI 兼容路由、SSE 流式编码、`/admin/*` 登录与管理 |
| `providers/base.py` | Provider 抽象基类：`generate` 流式接口 + 登录/串行队列/非流式聚合 |
| `providers/deepseek.py` | DeepSeek 网页版实现：登录、输入、双轨响应提取（XHR SSE 主 + DOM 兜底） |
| `providers/registry.py` | 模型名/别名 → Provider 路由；登录态检测（可用独立 headless 浏览器） |
| `browser/manager.py` | 浏览器生命周期：单 Chromium + 每 provider 独立 Context + storage_state 持久化 |
| `browser/extractor.py` | DOM → Markdown 提取、选择器候选匹配 |
| `core/queue.py` | 每 provider 串行门（SerialGate） |
| `core/threads.py` | 会话绑定（thread_id）生命周期 + 跨重启持久化 |
| `core/errors.py` | 统一错误类型 → HTTP 状态码映射 |
| `config.py` | YAML 配置加载 + 环境变量覆盖 |

---

## 3. Provider 抽象（核心扩展点）

```python
class BaseProvider(ABC):
    name: str                       # "deepseek"
    models: list[str]               # ["deepseek-web", ...]

    async def ensure_ready(self) -> None:
        """打开页面、恢复或建立登录态；未登录且无凭据时抛出明确错误"""

    async def chat(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        """非流式对话"""

    async def chat_stream(self, request: ChatCompletionRequest) -> AsyncIterator[StreamChunk]:
        """流式对话，产出 OpenAI SSE chunk"""

    async def reset(self) -> None:
        """新建对话（清空网页端上下文）"""

    async def close(self) -> None:
        """释放浏览器资源"""
```

**新增一个网页 AI 的步骤**（以未来 Kimi 为例）：
1. 新建 `providers/kimi.py`，继承 `BaseProvider`；
2. 实现 `ensure_ready`（含登录）、`chat` / `chat_stream`（选择器、等待完成策略）；
3. 在 `providers/__init__.py` 的 `PROVIDERS` 注册表中登记；
4. 配置新的 provider 段（`config.yaml`）即可切换/新增，HTTP 层零改动。

---

## 4. 浏览器与会话管理

### 4.1 登录态复用（关键）
网页 AI 登录大多有人机验证（滑块/短信），**不追求全自动登录**。采用「半自动」策略：

1. **首次登录**：`web2api login deepseek` 启动有头浏览器，用户手动登录（可过验证码）；
2. 登录成功后自动把会话导出为 `storage_state.json`（cookies + localStorage）；
3. **服务运行**：headless 浏览器启动时加载 `storage_state.json`，免登录直接可用；
4. **凭据自动登录**（可选）：配置了 `DEEPSEEK_USERNAME/PASSWORD` 时，若检测到未登录，尝试自动填表登录（密码登录 → 密码登录表单），失败则报错提示手动登录。

### 4.2 持久化与并发
- 使用 `launch_persistent_context(user_data_dir=...)`，浏览器 profile 落盘，进一步保证会话稳定；
- 网页对话是「单会话」模型：每个 Provider 内部用 `asyncio.Lock` 串行化请求，并发请求排队；
- 每次请求前点击 logo 新建对话 → 保证 API 语义的**无状态**（不同请求互不串上下文）。

---

## 5. DeepSeek 网页实现要点

### 5.1 已核实的 DOM 选择器（chat.deepseek.com，2026-08 实测）

| 目标 | 定位方式 |
|------|----------|
| 登录页判定 | URL 路径 `/sign_in` |
| 密码登录 tab | `get_by_role("button", name="密码登录")` |
| 账号输入框 | `get_by_role("textbox", name="请输入手机号/邮箱地址")` |
| 密码输入框 | `get_by_role("textbox", name="请输入密码")` |
| 登录按钮 | `get_by_role("button", name="登录")` |
| 对话输入框 | `get_by_role("textbox", name="给 DeepSeek 发送消息")` |
| 发送 | 输入后按 `Enter` |
| 助手回复节点 | 自定义元素 `dslc-reply-wrapper`，正文在 `dslc-markdown` |
| 回复完成判定 | 最后一个含 `dslc-markdown` 的 reply 内 `button` 数量 ≥ 5（出现复制/点赞等操作按钮） |
| 新建对话 | 导航区"开启新对话"（`text=开启新对话`） |
| 模式 | 2026-09 起三模式（快速/专家/识图）合并为单一模式：新对话页只有 toggle `深度思考` / `智能搜索`，请求体只有 `thinking_enabled` / `search_enabled`（`model_type` 恒为 default）。API 的 `mode` 字段翻译成开关组合（见 `DeepSeekProvider.MODE_PRESETS`） |

> 选择器集中在 `providers/deepseek.py` 顶部常量，DeepSeek 改版时只需更新常量。

### 5.2 一次请求的时序

```
ensure_ready()  ──►  页面就绪且已登录（否则尝试自动登录 → 失败报错）
点击 logo 新建对话（可选参数控制）
textbox.fill(最终消息)  →  press Enter
等待完成（waitForFunction: reply buttons ≥ 5，超时视为 partial）
提取 dslc-markdown.innerText()
流式模式：轮询 innerText 增量（200ms 间隔），产出 delta chunk
组装 OpenAI 响应返回
```

### 5.3 messages → 网页输入的映射
网页端只能提交「一段文本」，因此：
- 取 `messages` 中最后一条 `role == "user"` 的消息作为主输入；
- 若存在 `system` 消息，将其拼在用户输入前面（`[系统指令]\n用户输入`）；
- `assistant` 历史消息忽略（无状态 API 语义）。

### 5.4 完成判定与超时
- 默认超时 180s（`provider.response_timeout`，config.yaml 可改）；优先由网络监听通道的 `event: close` 判定完成，不可用时降级 DOM 轮询（停止按钮消失 / 文本稳定窗口）；
- 超时未完成 → 报 `504 response_timeout`，并已在客户端收到的流式增量不受影响。

---

## 6. OpenAI 兼容 API

### 6.1 端点
| 端点 | 说明 |
|------|------|
| `GET /v1/models` | 列出当前 provider 的模型 |
| `POST /v1/chat/completions` | 对话，`stream=true` 走 SSE |
| `GET /healthz` | 健康检查（含 provider 就绪状态） |

### 6.2 鉴权
可选：配置 `server.api_keys`（config.yaml）或环境变量 `WEB2API_API_KEY`（逗号分隔多 key）后，
所有 `/v1/*` 请求需带 `Authorization: Bearer <key>`，未配置则开放。
`/ui`、`/admin`、`/healthz` 不鉴权（本地管理用）；对外部署请用反代限制 `/admin`。

### 6.3 错误映射
- 未登录 / 登录失败 → `401 {"error": {"message": "...", "type": "authentication_error"}}`
- 网页操作失败 → `502`（bad_gateway，语义为上游网页异常）
- 请求参数错误 → `400`
- 流式中断 → SSE 正常结束并附 `error` chunk（OpenAI 兼容约定）

---

## 7. 配置项（环境变量 / .env）

> 配置以 `config.yaml` 为准；下表只列**真正生效**的环境变量覆盖（见 `config.apply_env_overrides`）。
> host / port / profiles_dir 等不提供 env 覆盖：`.env` 会被自动加载，若参与覆盖会导致
> “换 config 文件启动”（如 `AI_WEB2API_CONFIG=config.fake.yaml`）仍被 `.env` 里的端口霸占。

| 变量 | 默认 | 说明 |
|------|------|------|
| `AI_WEB2API_CONFIG` | `config.yaml` | 配置文件路径 |
| `AI_WEB2API_ENV_FILE` | — | 额外加载的 .env（覆盖进程环境） |
| `WEB2API_HEADLESS` / `<PROVIDER>_HEADLESS` | `true`（config.yaml） | 浏览器是否无头（优先级 WEB2API_HEADLESS > `<PROVIDER>_HEADLESS` > YAML） |
| `WEB2API_API_KEY` | 空 | 网关鉴权 key（可选，逗号分隔多 key），仅保护 `/v1/*` |
| `DEEPSEEK_USERNAME` / `DEEPSEEK_PASSWORD` | 空 | 可选：自动登录凭据（键名见 provider 的 `login.username_env/password_env`） |
| `WEB2API_PUBLISH_PORT` | `8000` | 仅 Docker Compose：宿主映射端口 |

---

## 8. 验证方式

1. **假页端到端**：`AI_WEB2API_CONFIG=config.fake.yaml` 启动服务（指向 `tests/fake_chat.html`，无需真实账号/登录），curl 验证 `/v1/models`、非流式、SSE 流式 —— CI 可用。
2. **DeepSeek 真实链路**：配 `.env` 凭据自动登录（或 `/admin/deepseek/login/start` 手动登录一次）→ 启动服务 → curl / OpenAI SDK 验证真实回复。
3. **pytest**：`tests/test_openai_compat.py` 用官方 openai SDK 对**已启动的服务**跑真实用例；其余测试为纯单元/假页端到端。

---

## 9. 已知限制 / 后续扩展

| 事项 | 现状 | 后续 |
|------|------|------|
| 多轮对话 | 无状态（每请求新对话） | 可按 `messages` 拼接完整对话文本 |
| 深度思考/搜索 | 可选参数映射 | 在请求扩展字段中透传 |
| 速率限制 | 依赖网页端自身限流 | 可加 per-provider 队列 |
| usage 统计 | 估算/空 | 通过 tokenizer 估算 |
| 新增 provider | — | Kimi、通义、智谱… 均只需新增子类 |
