# 通用 API Provider 接入指南

> 状态：**设计中**（待实现）。
> 目标：让非 Web UI 的 API（OpenAI 兼容 / 通义 / 智谱 / Kimi API 等）也能以 provider 身份接入，复用本项目的登录态、会话、指标、UI 体系。

## 1. 动机

现有 provider 都是 **Web UI 驱动**（`WebChatProvider`）：开浏览器 → 操作页面 → DOM 抓取。  
但很多免费 / 高性价比模型只有 API（无 Web UI，或 Web UI 反爬严）：

- GLM-4.7-Flash（智谱）— 免费，并发 1
- Qwen3-Flash / Qwen-Max（通义）— 免费额度
- DeepSeek API（已有 Web driver，但 API 更稳）
- Kimi API（月卡 + 按量）
- 任何 OpenAI 兼容端点（自定义代理、vLLM 等）

这些模型**不值得**开浏览器抢 Web UI，但值得接入。

## 2. 核心设计：新增 `driver: "api"`

### 2.1 架构

```
BaseProvider（不变）
├── WebChatProvider（不变）— 浏览器驱动
│   ├── DeepSeekProvider
│   ├── DoubaoProvider
│   ├── ...
└── APIProvider（新增）— HTTP API 驱动
    ├── glm-api（智谱）
    ├── qwen-api（通义）
    ├── openai-api（OpenAI 兼容端点）
    └── ...
```

- `APIProvider` 继承 `BaseProvider`，**不经过浏览器**。
- 复用 `BaseProvider.generate()` 签名、`StreamChunk`、`SerialGate` 队列。
- 复用指标系统：`metrics_store.record_metric()` 同样落盘。

### 2.2 生成流程

```
收到请求
  → 解析 api_key（${ENV} 语法）
  → 构造 OpenAI 兼容请求体
  → aiohttp POST（流式）
  → 逐行解析 SSE
    → data: {"choices":[{"delta":{"content":"..."}}]}
    → 提取 content / thinking
    → yield StreamChunk
  → 流结束 / 错误 → 生成结束
  → finally：记录指标（同 WebChatProvider）
```

### 2.3 配置扩展（`ProviderConfig` 新增字段）

```python
class ProviderConfig(BaseModel):
    # ... 现有字段不变 ...

    # API provider 专用（driver="api" 时必填）：
    api_base: str | None = None      # API 地址（覆盖 url）
    api_key: str | None = None       # API Key（支持 ${ENV_VAR}）
    api_model: str | None = None     # 默认模型
    api_timeout: float = 180.0       # 请求超时
    api_headers: dict[str, str] = {} # 额外请求头

    # 流式解析（适配不同 API 格式）：
    api_stream_prefix: str = "data: "        # SSE 行前缀
    api_done_marker: str = "[DONE]"          # 流结束标记
    api_content_path: list[str] = ["choices", "0", "delta", "content"]
    api_thinking_path: list[str] | None = None  # thinking JSON 路径（可选）
```

**API Key 环境变量语法**：

```yaml
api_key: ${GLM_API_KEY}   # 从环境变量读取
```

解析逻辑：`${VAR}` → `os.environ.get("VAR", "")`，未设置则为空字符串。

### 2.4 请求体构造

默认按 OpenAI 兼容格式：

```json
{
  "model": "glm-4-flash",
  "messages": [
    {"role": "user", "content": "你好"}
  ],
  "stream": true
}
```

非 OpenAI 格式可通过 `api_content_path` / `api_thinking_path` 调整 JSON 提取路径，无需改代码。

## 3. 接入步骤

### 3.1 在 `config.yaml` 添加 provider

```yaml
providers:
  - name: glm-api
    enabled: true
    driver: api
    url: https://open.bigmodel.cn/api/paas/v4/chat/completions
    api_key: ${GLM_API_KEY}
    api_model: glm-4-flash
    models:
      - {name: glm-4-flash, ui_label: "GLM-4.7-Flash"}
    proxy: false  # 国内直连
```

### 3.2 设置环境变量

```bash
export GLM_API_KEY="sk-..."
# 或写入 .env
```

### 3.3 重启服务

```bash
docker compose up -d  # 重新构建（新 driver 编译进镜像）
```

### 3.4 验证

```bash
curl -s -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"glm-4-flash","messages":[{"role":"user","content":"你好"}],"stream":false}'
```

## 4. UI 调整

API provider 在状态面板中：

- ❌ 不显示"登录"相关按钮（无需登录）
- ❌ 不显示"查看画面"（无浏览器）
- ❌ 不显示"登录态导入"
- ✅ 显示模型列表
- ✅ 显示指标卡片（成功率、耗时）
- ✅ 显示"退出登录"不适用

前端根据 `p.driver == "api"` 隐藏登录 UI。

## 5. 指标系统

API provider 与 WebChatProvider 共享同一套指标：

- `total`：请求总耗时
- `ttft`：首字延迟
- `success_rate`：成功率
- `finalize`：`api_timeout` / `exception` / `done`

无需额外实现，`BaseProvider.generate()` 的 finally 块已统一处理。

## 6. 限流与重试

免费 API 通常 QPS=1，当前 `QueueConfig.max_inflight=1`（默认）已串行化。  
如需重试（429/503），后续可加：

```yaml
api_retry:
  max_attempts: 3
  backoff_seconds: 2.0
```

MVP 阶段不实现，429 直接抛异常让客户端重试。

## 7. 非 OpenAI 格式适配

不同 API 返回格式不同，通过 JSON 路径配置提取：

| 站点 | content 路径 | thinking 路径 |
|---|---|---|
| OpenAI 兼容 | `choices.0.delta.content` | — |
| 智谱 | `choices.0.delta.content` | `choices.0.delta.reasoning_content` |
| 通义 | `output.choices[0].message.content` | `output.choices[0].message.content`（思考在 content 里） |
| vLLM | `choices.0.delta.content` | `choices.0.delta.reasoning_content` |

配置示例（通义）：

```yaml
api_content_path: ["output", "choices", "0", "message", "content"]
api_thinking_path: ["output", "choices", "0", "message", "content"]
```

## 8. 文件变更清单

| 文件 | 改动 |
|---|---|
| `src/ai_web2api/providers/api_provider.py` | 新增 `APIProvider` 类 |
| `src/ai_web2api/providers/registry.py` | 注册 `api` driver |
| `src/ai_web2api/config.py` | `ProviderConfig` 新增 API 字段 |
| `src/ai_web2api/api/routes.py` | `/admin/status` 返回 `driver` 字段 |
| `src/ai_web2api/webui/assets/js/index.js` | API provider 隐藏登录按钮 |
| `src/ai_web2api/webui/assets/css/index.css` | API provider 样式 |
| `docs/PROVIDER_API.md` | 本文档 |
| `tests/test_api_provider.py` | 单元测试 |

## 9. 待决定（已确认）

- **API Key 放 `.env`**：`api_key: ${GLM_API_KEY}`，明文在环境变量，不加密
- **Function Calling / Tools**：✅ 支持，模型本身支持
- **图片附件**：可选，V1 不做
- **非流式模式**：暂不需要
- **重试策略**：可配置 `api_retry.max_attempts`，默认 3 次，指数退避

## 10. 参考

- 智谱 API 文档：https://www.bigmodel.cn/dev/api
- 通义 API 文档：https://help.aliyun.com/zh/dashscope/
- OpenAI 兼容格式：https://platform.openai.com/docs/api-reference/chat
