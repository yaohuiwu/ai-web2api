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

`config.yaml` 里 `browser.headless` 默认 `false`（登录需要看到浏览器窗口，登录完可改 `true`）：

```bash
.venv/bin/python -m ai_web2api.main          # 启动服务
curl -X POST http://127.0.0.1:8000/admin/deepseek/login/start    # 打开登录窗口
# …… 在弹出的浏览器里完成登录（手机号验证码 / 密码）……
curl http://127.0.0.1:8000/admin/deepseek/login/status           # 检测登录结果
```

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

### 3. 自测（不需要登录）

仓库带一个假聊天页 + 假配置，把 DeepSeek 驱动完整跑一遍（含思考区提取、流式、Markdown 转换、超时/错误路径）：

```bash
AI_WEB2API_CONFIG=config.fake.yaml .venv/bin/python -m ai_web2api.main   # 端口 8001
curl http://127.0.0.1:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "fake-r1", "messages": [{"role": "user", "content": "你好"}]}'
```

## 配置

`config.yaml` 结构（完整字段见 `src/ai_web2api/config.py`）：

```yaml
server:
  host: 0.0.0.0
  port: 8000
browser:
  headless: false          # 首次登录用 false，之后可改 true
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

注意：登录页按浏览器语言渲染（中文选择器需 zh-CN 语言环境）；若触发验证码/风控卡在登录页，
改用 `login/start` 手动登录一次即可（登录态落盘后重启自动恢复）。

## 新增一个 Web AI

1. `providers/` 里写一个驱动类继承 `BaseProvider`，实现 `generate()`（打开页面 → 注入上下文 → 发送 → 轮询 diff 产出 `StreamChunk`）；若 DOM 结构跟 DeepSeek 类似，直接复用 `DeepSeekProvider`，只写配置
2. 在 `registry.DRIVERS` 登记驱动类
3. `config.yaml` 加一段 provider 配置（URL + 模型 + 选择器）

## 已知限制

- 多轮对话为**无状态模式**：每次请求把完整历史拼成一条 prompt 注入（借用 Web 端长上下文能力）；不做跨请求会话
- `max_tokens`/`top_p`/`stop` 等参数在 Web 端不可控，收到后忽略
- 数学公式（KaTeX）尽力还原，复杂排版可能失真
- 无 API key 鉴权（本地使用）；对外部署请自行加反代/鉴权
- Web 端改版会导致选择器失效，用 `/admin/{p}/debug/dom` 排查并更新配置
- 账号风控风险：请自用，控制频率

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
