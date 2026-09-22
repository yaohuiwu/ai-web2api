# UI 打磨 + 手动登录易用性（设计，待 review）

> 状态：**待 review**（未实现）。目标：① 让 `/ui` 看起来像一个正经开源项目；② 让手动登录的命令更好用。
> 约束：**只动静态资产 + CLI**，不改 OpenAI 兼容行为；每个阶段独立可验证、可回滚。

## 0. 现状盘点

| 资产 | 行数 | 问题 |
|---|---|---|
| `webui/index.html` | 47 | 无 favicon、无 footer、无文档/仓库入口、无主题切换 |
| `webui/playground.html` | 88 | 同上；工具条无分组、移动端拥挤 |
| `assets/css/base.css` | 24 | 无设计令牌（颜色/间距/圆角/阴影/字号），无暗色 |
| `assets/css/index.css` | 142 | 硬编码色值（`#fde68a`/`#92400e`…），无响应式 |
| `assets/css/playground.css` | 190 | 同上 |
| `assets/js/*.js` | 1058 | 结构清晰；缺「复制/耗时/原始报文」等交互 |

结论：**不需要重写框架**，补齐"设计系统 + 信息层次 + 开源项目该有的入口"即可。

---

## Part A — UI 打磨

### A0. 设计系统（1 提交，基础）
- 新增 `assets/css/tokens.css`：`:root` 定义颜色（bg/surface/border/text/muted/primary/ok/warn/danger）、间距（4/8/12/16/24）、圆角、阴影、字号、`--mono` 字体栈。
- 暗色：`[data-theme="dark"]` 覆盖同一组变量；默认跟随 `prefers-color-scheme`，用户选择存 `localStorage`。
- 组件类收敛：`.btn`(`.primary/.ghost/.danger/.sm`)、`.card`、`.panel`、`.badge`(`.ok/.warn/.no`)、`.chip`、`.kv`、`.tabs/.tab`、`.input/.select`、`.tooltip`。
- 清掉三份 CSS 里散落的硬编码色值，全部走变量。
- **验收**：`grep -nE '#[0-9a-fA-F]{3,6}' assets/css/*.css` 只剩 tokens.css 里有色值。

### A1. 全局框架（1 提交）
- 统一 `<header>`：logo（内联 SVG）+ 名称 + 版本 + 服务地址；导航「状态面板 / Playground / API 文档(`/docs`)」；右侧：主题切换 ☀️/🌙、GitHub/仓库链接（占位可配）。
- 统一 `<footer>`：版本、`/docs`、`/healthz`、`/v1/models`、`README` 链接。
- `favicon.ico`/`icon.svg` 用 **data URI 内联**（不引 CDN、不落二进制）。
- 响应式：`@media (max-width: 820px)` 单列布局；KPI 网格自动换行；聊天区用 `dvh`（移动端键盘不遮挡）。

### A2. 状态面板（1 提交）
- KPI 卡片：图标 + 语义色 + 悬浮说明（覆盖 `status_check_headless`、`thread_persist`、`api_keys_configured` 等）。
- Provider 卡片：
  - 状态点 + 徽章（已登录/未登录/即将过期）；
  - **认证有效期进度条**（剩余 / 推算有效期；`soon` 黄、`expired` 红）——把已有的 `days_left`/`validity_days` 用起来；
  - 摘要行：模型数 · 别名数 · 活跃 thread 数。
- 空状态：无 provider / 无 thread 时给出引导文案（而非空白）。
- 登录区：命令块带复制；**步骤 1-2-3**；失败截图可放大 + 「重试自动登录」。
- Thread 表：thread_id 可复制、相对时间、provider 徽章、**「去 Playground 继续」**（带 thread_id 跳转）。
- 按钮 loading 态（请求中禁用 + 转圈），避免重复点击。

### A3. Playground（1 提交）
- 工具条分组：`模型 / 会话 / 选项 / 工具`，窄屏可折叠。
- 消息：角色标签 + **时间戳** + **耗时**（首 token 延迟 / 总耗时）+ 复制正文 + 复制原始 JSON + 重新生成。
- 侧栏会话：搜索框、相对时间、provider 徽章（删除/重命名视后端支持）。
- 「原始请求/响应」抽屉（`rawLog` 已有数据）+ **复制为 curl**。
- 快捷键说明（Enter 发送 / Shift+Enter 换行）。

### A4. Quickstart 接入区（1 提交，"开源感"最强的一块）
- 状态面板底部：按当前 `location.origin` 生成
  - `curl` 示例、OpenAI SDK（Python / Node）片段，各带「复制」；
  - 链接：`/docs`（Swagger）、`/v1/models`、`/healthz`。
- 若配置了 `server.api_keys`，提示需带 `Authorization: Bearer`。

**风险**：全部静态资产；后端零改动。验证 = 静态断言（`tests/test_webui_assets.py`）+ Playwright 截图/断言。

---

## Part B — 手动登录更好用

### B0. 现状痛点
1. 命令长（`python -m ai_web2api.cli login chatgpt`），要记 provider 名；
2. 省略 provider 时会静默用默认值，选错也不提示；
3. **Docker 下容器里 headful 看不到窗口** → 必须在**宿主**跑并把 state 导进服务；
4. 导入地址/API Key 要人肉拼；
5. 成功/失败/下一步不直观。

### B1. 一行命令 + 控制台入口（1 提交）
- `pyproject.toml` 增加 `[project.scripts] ai-web2api = "ai_web2api.cli:main"` → 之后 `ai-web2api login`。
  （`python -m ai_web2api.cli` 保持可用，向后兼容。）
- **交互式选 provider**：省略 `<provider>` 时列出候选并标注 `mode(manual/auto)`、当前登录态、认证剩余天数，让用户数字选择。
- **导入地址自动推导**：`--import-url` > `AI_WEB2API_URL` 环境变量 > config 推导；并对 Docker 场景提示 `--import-url http://localhost:8000`。
- 结束**明确输出**：成功/失败、provider、登录时间、推算有效期；失败给命令 + 截图路径。

### B2. 便捷脚本（1 提交）
- `scripts/login.sh [provider]`（+ `scripts/login.ps1`）：
  - 自动挑解释器（`uv run` → `.venv/bin/python` → `python3`）；
  - 自动定位仓库根/config；导出 `AI_WEB2API_URL`；
  - 例：`./scripts/login.sh chatgpt` 一条命令完成「开宿主浏览器 → 登录 → 导入到容器」。
- `README` 给三种场景的**一行命令**：本机 venv / Docker(宿主脚本) / 远程服务(`--import-url`)。

### B3. UI 深度集成（1 提交）
- 登录卡片改成**三 Tab**，每 Tab 一段可复制命令，自动填 `--import-url <当前 origin>`：
  1. **本机**：`ai-web2api login chatgpt`
  2. **Docker**：`./scripts/login.sh chatgpt`（并说明"在宿主执行，窗口才可见"）
  3. **手动粘贴/拖拽** state.json（已有）
- 展示 `login_at`（已有字段）：「本次登录时间 / 上次导入来源」，让用户确认导入生效。

### B4. 不推荐
- 服务端把浏览器画面流给前端（noVNC 等）：复杂度高、维护贵，收益低于 B1+B3。

---

## 实施顺序与提交拆分（建议）

| # | 提交 | 内容 |
|---|---|---|
| 1 | `ui: design tokens + dark mode` | A0 |
| 2 | `ui: header/footer/favicon/responsive` | A1 |
| 3 | `ui: status panel polish` | A2（含有效期进度条） |
| 4 | `ui: playground polish` | A3 |
| 5 | `ui: quickstart snippets` | A4 |
| 6 | `cli: console script + interactive picker + url autodetect` | B1 |
| 7 | `scripts: login.sh + README 一行命令` | B2 |
| 8 | `ui: login tabs (local/docker/paste)` | B3 |

## 待确认

1. **UI 范围**：全做（1–5），还是先做 1–3（基础+状态面板）？
2. **暗色主题**要不要（开源项目标配，成本中等）？
3. **Quickstart（curl/SDK 片段）**要不要？
4. 手动登录：接受新增 console script `ai-web2api` + `scripts/login.sh` 吗？
5. UI 里要不要区分「本机 / Docker」两套登录命令（B3）？
