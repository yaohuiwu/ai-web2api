# 手动登录：CLI 子命令 + state 导入（设计，待评审）

> 状态：**设计，待批准**。批准后再改代码。
> 目标：在**有显示器的本机**用真实浏览器手动完成登录（含验证码/Google/短信），
> 生成 `storage_state`，再导入到运行中的服务（含 Docker），无需 VNC / 改镜像。

## 1. 目标与边界

- **能手动登录**：打开有头浏览器，用户自己点（Google/Apple/验证码都行）。
- **自动填表省事**：默认先用 `.env` 凭据自动填账号密码，用户只需补验证码/选 Google；
  `--manual` 可关掉自动填表。
- **一条命令导入**：登录成功后生成 `state.json`，可自动 POST 给运行中的服务（Docker 友好）。
- **不动 Dockerfile / 不加 VNC**。
- 不做：在容器内跑有头浏览器、把浏览器画面嵌进 `/ui`。

## 2. 命令形态

```bash
# 在项目根、宿主上运行（用项目 .venv + Playwright）
python -m ai_web2api.cli login qwen
python -m ai_web2api.cli login deepseek --manual
python -m ai_web2api.cli login               # provider 省略 → server.default_provider 或第一个启用者
python -m ai_web2api.cli login qwen --no-import        # 登录完只写 state，不自动导入
python -m ai_web2api.cli login qwen --import-url http://127.0.0.1:8000   # 覆盖导入地址
```

> **默认登录成功后自动导入本地服务**（地址由 `config.yaml` 的 `server.host/port` 推导，
> `0.0.0.0` → `127.0.0.1`）。导入失败（服务没起）不会报错，只提示可直接用的 state 文件。

参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `provider`（位置） | `server.default_provider` / 首个启用 | provider 名 |
| `--manual` | 否 | 不自动填表，纯手动 |
| `--timeout` | `600` | 等待"登录成功"的最长秒数 |
| `--out` | `profiles/<p>/state.json` | state 输出路径 |
| `--import/--no-import` | **导入** | 登录成功后自动 POST 给服务；`--no-import` 关闭 |
| `--import-url` | 本地（由 config 推导） | 覆盖导入地址 |
| `--import-key` | `WEB2API_API_KEY` 环境 | 若服务启用 API key（`/admin` 默认不鉴权，通常不需要） |

## 3. 登录流程（CLI）

```
构造 config（browser.headless 强制 false）→ BrowserManager.start()
→ registry.get_provider(p)
→ 复用 provider 配置：login_url / login_check_selectors / login.page 选择器 / 凭据 env
→ 打开页面（沿用 provider 的 context 与已有 state）：
    1) goto login_url（用 provider._goto_ready：卡加载屏自动 reload）
    2) 已登录？(login_check) → 直接保存 state，结束
    3) 切"密码登录"tab（若默认是验证码 tab）
    4) 非 --manual 且有凭据 → 逐字输入账号/密码（type，不用 fill）
    5) 打印提示："请在弹出的浏览器里完成验证码 / Google 登录 …"
    6) 轮询 login_check，命中 → 保存 storage_state 到 --out
→ 默认：POST state 到本地服务的 /admin/<p>/login/state（--no-import 可关）
  导入失败（服务未启动）→ 仅提示 state 文件路径与手动导入命令，不算失败
```

要点：
- **逐字输入**（`press_sequentially`）：Qwen 是 React 受控输入，`fill()` 不生效（已踩过）。
- 复用 `BaseProvider._goto_ready` / `_wait_loading_gone` / `login_check_selectors`（含 Qwen 的 reload 重试）。
- Ctrl+C 前不保存；只有检测到登录成功才落盘，避免写入半成品。

## 4. 导入接口（服务端）

新端点：`POST /admin/{provider}/login/state`

- Body：完整 Playwright storage_state，`{"cookies": [...], "origins": [...]}`（origins 可选）。
- 行为：
  1. 校验基本结构；
  2. **原子写入** `profiles/<provider>/state.json`（复用 `BrowserManager.state_path`）；
  3. **重置该 provider 的 context**（`BrowserManager.reset_context`）：关闭并丢弃内存里的 context，
     下次 `get_context` 自动从新 `state.json` 重建 → **无需重启服务**；
  4. `check_login()` 复核 → 更新登录态（成功则 `mark_state_dirty` + `save_state`）；
  5. 若该 provider 有活跃 thread 会话，其页面属于旧 context → 一并失效（见 §6）。
- 返回：`{"provider": p, "logged_in": bool}`（+ `inconclusive` 可选）。
- 兼容：已有 `POST /admin/{p}/login/cookies` 保留（只导 cookie）。

`BrowserManager` 新增：
- `async def reset_context(self, provider: str) -> None`：`ctx.close()` + 从 `_contexts` 移除 + 清理 `_state_dirty`。

## 5. UI（状态面板）

- 「手动登录」按钮改为**弹出提示**，展示：
  - 本机命令：`python -m ai_web2api.cli login <provider>`
  - 一个「粘贴 `state.json` 导入」文本框 → 调 `POST /admin/<p>/login/state`
    （给"脚本在别的机器上跑"的兜底；Docker 用户可复制文件内容粘贴）。
- 登录成功后刷新面板（已有）。

## 6. 兼容性 / 风险

- **Docker**：脚本在宿主跑，`--import-url http://127.0.0.1:8000` 直接导入容器里的服务（端口已映射）。
- **context 重置**会使该 provider 的活跃 thread 页面失效 → 后续同 thread 请求按"页面失效"处理并重建；
  导入时可选：主动 `threads.close` 掉该 provider 的活跃会话（更干净）。
- `state.json` 含 cookies/localStorage，属敏感数据；`profiles/` 已 gitignore / dockerignore，勿外传。
- 有头浏览器需要宿主有 Playwright 浏览器：`pip install playwright && playwright install chromium`
  （项目 `.venv` 通常已具备）。
- Google 登录成功率取决于出口网络；失败可直接改用 `--manual` 多试或导入 cookies。

## 7. 分步实现计划（每步一提交）

- **Step 1** — `BrowserManager.reset_context()` + 单测（关 context、下次重建）。
- **Step 2** — `POST /admin/{p}/login/state`（写盘 + reset + 复核）+ TestClient 单测。
- **Step 3** — `ai_web2api/cli.py`（argparse 子命令 `login`）+ 流程；单测覆盖参数解析与"保存/导入"分支（网页交互用 stub）。
- **Step 4** — UI：手动登录提示 + 粘贴导入；文档（README「手动登录」、PROVIDER_QWEN 关联）。
- （可选）**Step 5** — 登录成功日志/`/healthz` 复核；`--out` 覆盖已有 state 的备份策略。

## 8. 已定 / 待确认

已定：
- 子命令模块：`python -m ai_web2api.cli login …`。
- **登录成功默认自动导入本地服务**（`--no-import` 关闭，`--import-url` 覆盖）。
- 保留现有 `POST /admin/{p}/login/cookies`。

我拟采用的默认（如无异议即按此开发）：
1. 导入时**主动关闭该 provider 的活跃 thread 会话**（避免旧 context 页面残留）。
2. UI 加「粘贴 `state.json` 导入」文本框（兜底：脚本在别的机器跑）。
