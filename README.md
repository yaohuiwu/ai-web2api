# ai-web2api

把各种 **Web 端 AI 聊天产品**（DeepSeek、Kimi、通义千问、ChatGPT 等）封装成 **OpenAI 兼容 API** 的服务。

核心机制：Playwright 驱动真实浏览器 —— 打开网页、保持登录态、输入消息、增量提取流式响应，再以 OpenAI 的 `/v1/chat/completions` 格式暴露出去。不逆向任何内部 API，纯 DOM 自动化，Web 改版只需改配置里的选择器。

> 设计文档见 [`docs/DESIGN.md`](docs/DESIGN.md)。当前进度：M1（DeepSeek 驱动 + 非流式/流式 + 登录态持久化 + 串行队列）已完成。

## 快速开始

```bash
uv sync                      # 安装依赖（创建 .venv）
.venv/bin/python -m playwright install chromium   # 安装浏览器
```

### 1. 登录（首次必做）

推荐方式：`config.yaml` 里配 `login.mode: auto` + `.env` 写 `DEEPSEEK_USERNAME/DEEPSEEK_PASSWORD`，
服务启动时自动登录（无头，不弹窗口）。

需要**手动**登录（自动登录被验证码/风控拦住）时，临时让窗口可见再重启：

```bash
DEEPSEEK_HEADLESS=false .venv/bin/python -m ai_web2api.main   # 或写进 .env 后重启
curl -X POST http://127.0.0.1:8000/admin/deepseek/login/start    # 打开登录窗口
# …… 在弹出的浏览器里完成登录（手机号验证码 / 密码）……
curl http://127.0.0.1:8000/admin/deepseek/login/status           # 检测登录结果
```

> `browser.headless` 默认 `true`（静默运行，聊天不再弹出浏览器窗口）。
> 覆盖优先级：`WEB2API_HEADLESS` > `DEEPSEEK_HEADLESS`（即 `<PROVIDER>_HEADLESS`）> `config.yaml`。
> headless 下 `login/start` 打开的窗口不可见，接口会直接返回提示而不是静默卡住。

登录态自动保存到 `profiles/deepseek/state.json`，重启服务自动恢复（无需重复登录）。

也可以直接导入 cookies：

```bash
curl -X POST http://127.0.0.1:8000/admin/deepseek/login/cookies \
  -H 'Content-Type: application/json' \
  -d '{"cookies": [{"name": "...", "value": "...", "domain": ".deepseek.com"}]}'
```

### 2. 调用（OpenAI 兼容）

非流式：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-web", "messages": [{"role": "user", "content": "讲个笑话"}]}'
```

流式（SSE）：

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-r1-web", "messages": [{"role": "user", "content": "1+1=?"}], "stream": true}'
```

OpenAI SDK 直接可用：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused")
resp = client.chat.completions.create(
    model="deepseek-web",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

### 2.5 会话绑定（thread_id，可选）

默认**无状态**：每次请求开新会话，历史由客户端在 `messages` 里带全。需要"同一 Web 会话多轮"时传 `thread_id`：

```bash
# 第一次：创建会话（注入 messages 全部历史）
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-web", "thread_id": "my-chat", "messages": [{"role": "user", "content": "我叫小明"}]}'

# 后续：同 thread_id 复用同一页面；只发最后一条 user 消息，历史以页面为准（无需重传）
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-web", "thread_id": "my-chat", "messages": [{"role": "user", "content": "我叫什么名字"}]}'
```

- `thread_id` 由客户端自定义（服务端不生成，只回显确认）：非流式响应的 `thread_id` 字段、SSE 每个 chunk 的 `thread_id` 字段，均回显本次绑定的 id；不带 thread_id 时为 `null`
- 也支持 `X-Thread-Id` header；OpenAI SDK 传非标准字段用 `extra_body={"thread_id": "..."}`（或 `extra_headers={"X-Thread-Id": "..."}`）
- 同一 `thread_id` 串行执行；不同 thread 并行（`server.thread_parallel: false` 可退回全局串行）
- 页面空闲超过 `server.thread_ttl`（默认 900s）自动回收；活跃会话上限 `server.max_threads`（默认 8，超限 429）
- 管理：`GET /admin/threads`（列表）、`DELETE /admin/threads/{id}`（强杀，客户端下次同 id 请求自动重建）
- 同一 thread 切换 model 会报 409（创建时绑定 provider+model）
- 页面忙（上一请求未完成，新消息被 Web 端排队）→ 20s 内返回 409 `thread_busy` 并销毁会话（配置 `thread_busy_timeout`），客户端稍后重试即自动重建；上一请求未释放（客户端中断）→ 60s 内返回 504 `thread_timeout` 并销毁。请求均**有界**，不会无限挂起
- **跨重启持久化**（`server.thread_persist: true`，默认开）：每次请求完成后，服务端把绑定页的 DeepSeek 会话 id（URL 末段 `/a/chat/s/<uuid>`）落盘到 `profiles/<provider>/threads.json`。服务重启后，同 `thread_id` 的请求会自动 `goto` 该会话 URL 恢复——**多轮记忆跨重启保持**（DeepSeek 不删用户会话）。TTL 空闲回收/服务关闭**保留**恢复能力；`DELETE /admin/threads/{id}`、页面失效/超时/忙错误**清除**（下次同 id 开全新会话）。恢复时切换 model 同样 409

### 2.6 Web 端选项：模式 + 开关（provider 通用）

> **2026-09 UI 变更**：DeepSeek 把「快速模式 / 专家模式 / 识图模式」三个模式合并为**单一模式**，
> 新对话页只剩两个开关（深度思考 / 智能搜索），请求体只有 `thinking_enabled` / `search_enabled`
> （`model_type` 恒为 `default`）。API 的 `mode` 参数保留兼容：不再点 radio，而是**翻译成开关组合**。

API 用通用字段（OpenAI SDK 用 `extra_body` 传），provider 各自映射自己的 UI：

```python
resp = client.chat.completions.create(
    model="deepseek-web",
    messages=[{"role": "user", "content": "帮我看下这张图的代码"}],
    extra_body={
        "mode": "expert",       # fast / expert / image 兼容值：翻译成开关组合（每次请求生效）
        "deep_think": True,     # 深度思考开关（每次请求生效）
        "search": False,        # 智能搜索开关（每次请求生效）
    })
```

- `mode`（新版 UI 语义）：`fast` = 思考关 + 搜索关，`expert` = 都开，`image` = 都关（附件已不限模式）。
  翻译只在该参数未显式给出时生效——`deep_think` / `search` 优先级更高；未知 mode 值忽略并记日志（不再 400）
- `mode`（旧版 UI，`selectors.mode_button` 非空时）：仍按原语义点 radio，仅新会话生效，resume 时忽略；
  未知值 400 `unsupported_mode`
- `deep_think` / `search`：每次请求生效（已处于目标状态则不点击），thread 续用/恢复时同样可切换。
  新版 UI 页面默认两个开关**都是开**；页面无对应开关时自动跳过
- 开关是页面级 UI 状态：**并行 thread 同时使用不同开关参数可能互相影响**（同一浏览器 context 共享开关状态），固定设置时无影响；`thread_parallel: false` 可完全避免
- 页面语言固定 `browser.locale: zh-CN`（选择器文案是中文；DeepSeek 按 `Accept-Language` 渲染 UI）——否则 Playwright 默认 en-US 会让「深度思考 / 开启新对话」等选择器全部失配
- 选择器全部配置化：`selectors.mode_button`（API 值 → 候选，新版 UI 留空）、`mode_checked`、`toggle_button`（字段名 → 候选，中英文各一份）、`toggle_checked`（见 config.yaml）

### 2.7 附件上传（图片识别，provider 通用）

DeepSeek 输入框左下角附件按钮任何会话都可用（三模式合并后不再限模式；仅识别图片中的文字，最多 50 个、每个 100MB）。API 用 OpenAI 标准的多部分 `content` + `image_url` 表达（OpenAI SDK 原生支持）：

```python
resp = client.chat.completions.create(
    model="deepseek-web",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "这张图里写了什么？"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,<base64>"}},
        ],
    }],
)
```

- `image_url.url` 支持 **data URL**（`data:image/png;base64,...`）和 **http(s) 外链**（服务端下载后上传）
- 纯图片消息（无 text 部分）也可以发送（Web 端上传后即使输入框为空也能发送）
- 服务端把附件解码为临时文件 → 通过页面 `input[type=file]` 上传 → 随消息发送；`selectors.upload_input` 配置化（找不到上传入口 → 400 `attachments_error`）
- 限制：**最多 50 个、每个最大 100MB**，超限 400 `attachments_error`（与 Web 端 tooltips 一致）
- 附件仅作用于本次请求的 user 消息（thread 续用时注入的历史为纯文本，图片不会重放）；页面找不到上传入口（如后续 UI 再改版）→ 400

### 3. 自测（不需要登录）

仓库带一个假聊天页 + 假配置，把 DeepSeek 驱动完整跑一遍（含思考区提取、流式、Markdown 转换、超时/错误路径）：

```bash
AI_WEB2API_CONFIG=config.fake.yaml .venv/bin/python -m ai_web2api.main   # 端口 8001
curl http://127.0.0.1:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "fake-r1", "messages": [{"role": "user", "content": "你好"}]}'
```

**OpenAI SDK 真实 API 测试**（`tests/test_openai_compat.py`，16 个用例，跑**已启动的服务**、真登录 DeepSeek）：

```bash
.venv/bin/python -m ai_web2api.main                                    # 先起服务（默认 127.0.0.1:8000）
.venv/bin/python -m pytest tests/test_openai_compat.py -v              # 另开终端
```

服务未启动或未登录时整模块 skip；`AI_WEB2API_BASE_URL` 可指向别的端口。用例结束会 DELETE 掉自己新建的会话（不动你原有的会话）。
覆盖：非流式/流式对话、多轮历史、`/v1/models`、模型别名（`gpt-4`）、未知模型 404、`thread_id` 绑定（create/resume 回忆上下文 + `X-Thread-Id` header）、深度思考开关（开 → `reasoning_content`，关 → 无）、`mode=expert` 预设、未知 mode 不再 400、附件上传（`image_url` data URL，含多附件与 51 个超限 400）、流式 + 附件组合。

## 配置

`config.yaml` 结构（完整字段见 `src/ai_web2api/config.py`）：

```yaml
server:
  host: 0.0.0.0
  port: 8000
browser:
  headless: true           # 静默运行（不弹窗口）；可用 .env 覆盖：WEB2API_HEADLESS > DEEPSEEK_HEADLESS
  locale: zh-CN            # 页面语言（决定 DeepSeek UI 文案 / 中文选择器是否匹配）
  login_check_interval: 300  # 定时检测登录态间隔（秒）
  state_expiry_margin: 86400 # 登录态剩余有效期低于该值才落盘 state.json（秒）
                             # 已登录且未过期就不写盘：只有「登录态刚变化 / 还没落盘 / cookie 快过期」才 save
  status_check: true         # 定时状态检测总开关
  status_check_headless: true  # 检测用独立 headless 浏览器，不弹出/占用主浏览器窗口（默认开）
profiles_dir: profiles     # 登录态持久化目录

providers:                 # 也支持 list 写法
  deepseek:                # ← provider 名（同时决定驱动类）
    url: https://chat.deepseek.com
    models:
      - {name: deepseek-web, ui_label: "DeepSeek 最新版"}
      - {name: deepseek-r1-web, ui_label: "DeepSeek-R1"}
    selectors:             # Web 改版只改这里；每个字段是"候选列表"，取第一个匹配的
      input:               # 2026-08 新 UI 实测：textarea[name=search]
        - "textarea[name=search]"
        - "textarea"
        - "#chat-input"
      send_button: []              # 空 = 回车发送
      response_container:
        - "[class*=markdown]"      # 新 UI 实测 ds-markdown 仍在
        - ".ds-markdown"
      thinking_container:
        - "[class*=think]"
        - ".ds-think"
      stop_button: []              # 填了可加快"生成结束"判定
      login_check: []              # 空 = 用 input 判定登录
    login:
      mode: auto                  # auto = 用 .env 凭据自动登录；manual = 手动弹窗
      username_env: DEEPSEEK_USERNAME
      password_env: DEEPSEEK_PASSWORD
      page:
        password_tab:             # 默认是验证码 tab 时，切到"密码登录"
          - "div[role=button]:has-text(\"密码登录\")"
        username:                 # 2026-08 实测：无 id/name，placeholder 定位
          - "input[placeholder=\"请输入手机号/邮箱地址\"]"
          - "input[placeholder*=手机号]"
          - "input[type=text]"
        password:
          - "input[placeholder=\"请输入密码\"]"
          - "input[type=password]"
        submit:
          - "div.ds-button--primary"
          - "button[type=submit]"
    queue: {max_size: 10, timeout: 60}   # 每 provider 串行队列
    response_timeout: 180
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/models` | 模型列表 |
| POST | `/v1/chat/completions` | 聊天补全（`stream` 走 SSE） |
| GET | `/healthz` | 健康检查 + 各 provider 登录态 |
| POST | `/admin/{p}/login/auto` | 自动登录（读 .env 凭据，见下） |
| POST | `/admin/{p}/login/start` | 打开登录窗口（手动登录） |
| GET | `/admin/{p}/login/status` | 查询/确认登录并保存状态 |
| POST | `/admin/{p}/login/cookies` | 导入 cookies |
| POST | `/admin/{p}/login/logout` | 清除登录态 |
| GET | `/admin/{p}/debug/dom?selector=…` | 调试：返回页面元素 HTML（排查选择器失效） |
| POST | `/admin/{p}/debug/probe` | 调试：发测试消息并 dump 响应区 DOM（确定新 UI 容器选择器） |
| GET | `/admin/threads` | 会话绑定：活跃会话列表 |
| DELETE | `/admin/threads/{id}` | 会话绑定：强杀会话（同 id 下次请求自动重建） |

### 自动登录（login.mode=auto）

在项目根 `.env` 配置（键名见 `config.yaml` 的 `login.username_env/password_env`，默认
`DEEPSEEK_USERNAME` / `DEEPSEEK_PASSWORD`，也兼容 `username` / `password`）：

```bash
DEEPSEEK_USERNAME=你的账号
DEEPSEEK_PASSWORD=你的密码
```

然后：

```bash
curl -X POST http://127.0.0.1:8000/admin/deepseek/login/auto   # 自动填表登录（约 15s）
curl http://127.0.0.1:8000/admin/deepseek/login/status         # 确认 logged_in: true
```

**启动时自动登录**：若某 provider 无 `profiles/<name>/state.json`（未登录），且 `login.mode=auto`、
.env 已配置凭据，服务启动时会自动尝试登录（日志可见"未登录，尝试自动登录…"→"自动登录成功"）；
失败不阻塞启动（验证码/风控时改用 `login/start` 手动登录一次）。已有 state.json 时直接恢复，
不会重复登录。

注意：登录页按浏览器语言渲染，服务已固定 `browser.locale: zh-CN`（并显式发送
`Accept-Language`），中文选择器（"密码登录"/"请输入手机号/邮箱地址"）才匹配；若触发验证码/风控卡在
登录页，改用 `login/start` 手动登录一次即可（登录态落盘后重启自动恢复）。

## 新增一个 Web AI

1. `providers/` 里写一个驱动类继承 `BaseProvider`，实现 `generate()`（打开页面 → 注入上下文 → 发送 → 轮询 diff 产出 `StreamChunk`）；若 DOM 结构跟 DeepSeek 类似，直接复用 `DeepSeekProvider`，只写配置
2. 在 `registry.DRIVERS` 登记驱动类
3. `config.yaml` 加一段 provider 配置（URL + 模型 + 选择器）

## 已知限制

- 多轮对话默认**无状态模式**：每次请求把完整历史拼成一条 prompt 注入新会话（借用 Web 端长上下文能力）；需要跨请求会话时用 `thread_id`（见上文 2.5，复用同一 Web 页面，历史以页面为准）
- `max_tokens`/`top_p`/`stop` 等参数在 Web 端不可控，收到后忽略
- 数学公式（KaTeX）尽力还原，复杂排版可能失真
- 无 API key 鉴权（本地使用）；对外部署请自行加反代/鉴权
- Web 端改版会导致选择器失效，用 `/admin/{p}/debug/dom` 排查并更新配置
- 停止服务：Ctrl+C 会给**整个进程组**发信号，Playwright 的 node 驱动同时被打掉，浏览器已无法优雅关闭
  → 服务打一条 WARNING（`browser.close 失败（驱动可能已退出，忽略）`）后正常退出，不会报
  `Application shutdown failed`；登录态早已按需落盘，不影响下次启动
- 账号风控风险：请自用，控制频率
- llama_index.llms.openai 兼容：role 支持 `developer`/`tool`/`function`（`developer` 自动映射为
  `system`），`content` 支持多部分列表（提取 text 部分），`tool_calls` 等字段自动忽略；
  注意 llama_index 客户端对非官方 OpenAI 模型名有校验与 tokenizer 限制（传 `max_tokens`
  可跳过 tokenizer 计数，老版本则无此问题）

## 项目结构

```
src/ai_web2api/
├── main.py            # FastAPI 入口 + 后台登录态刷新
├── config.py          # YAML → Pydantic 校验（兼容 list/dict 两种 provider 写法）
├── api/               # OpenAI 兼容路由、schema、SSE
├── browser/           # 浏览器管理（单实例多 Context + storage_state 持久化）、DOM→Markdown 提取
├── providers/         # 驱动基类 + DeepSeek 实现 + 注册表
└── core/              # 串行队列（SerialGate）、错误类型 → HTTP 状态码
```
