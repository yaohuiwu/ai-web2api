# 交互组件（iframe widget）捕获与 Playground 呈现（设计，待 review）

> 状态：**待 review**（未实现）。已完成的**兜底**：正文末尾附 `[🧩 交互组件](url)` + 组件可见文本
> （`selectors.include_frames: true`，见 `tests/test_extract_multi.py`）。
> 本文解决"**要完整截图 / 要能交互的 HTML**"。

## 1. 结论（可行性已实测）

**可行。**在真实的 Kimi「地图规划」组件上实测（2026-09）：

| 项 | 实测值 | 说明 |
|---|---|---|
| `frame.content()`（跨域 iframe 的完整 HTML） | **28,292 B** | Playwright 不受同源策略限制，能直接读出 |
| 自包含程度 | 内联 `<style>`×4、`<script>`×5、`<svg>`×2 | **外部 script/link/img = 0**，**无 fetch/XHR** |
| `iframe.screenshot()` / `frame.locator("body").screenshot()` | **56–60 KB PNG** | 主文档定位 iframe 也能截 |
| 同页 canvas frame 数 | 4（2 个组件 × 2 轮） | → 必须按"**本轮新增**"筛选（已有 frame URL 基线） |

**因此两条路都能走，且互补**：
- **HTML（推荐）**：自包含 → 塞进 `srcdoc` + `sandbox="allow-scripts"` 的 iframe 即可**交互**（Day 标签可点），无需网络、无 CORS 问题；
- **PNG（保真兜底）**：任何组件都能给"所见即所得"的图（哪怕 HTML 依赖运行时/外链而渲染失败）。

## 2. 数据流

```
发送前：记录 frame URL 基线（已有）
   ↓ 本轮生成
检测到新 frame → 产出两种产物
   ├─ HTML：frame.content()（≤ 512KB 截断）
   └─ PNG ：frame.locator("body").screenshot()（≤ 2MB 截断）
   ↓
存储（与消息关联）→ 接口暴露 → Playground 渲染 / API 返回
```

### 2.1 存储（建议）

```
profiles/<provider>/widgets/<thread_id>-<turn>-<n>.html|.png
```
- 与消息关联：在 `messages` 表加一列（或复用 `attachments` JSON）存 widget 的**元数据**
  （`{"id","kind","bytes"}`），文件本体落盘 —— 避免 DB 膨胀，也便于直接 `<iframe src>` 加载。
- 清理：随 thread 删除（`DELETE /admin/threads/{id}`）一并删除；`profiles/` 已有 TTL 语义。

### 2.2 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/{p}/widgets/{id}.png` | 组件截图（`Cache-Control: no-store`，id 白名单校验） |
| GET | `/admin/{p}/widgets/{id}.html` | 组件 HTML（`Content-Type: text/html; charset=utf-8`） |
| GET | `/admin/threads/{id}/messages` | 每条消息追加 `widgets: [{id, png, html, bytes}]`（无则 `[]`） |

`/v1/chat/completions` 返回体（**非标准字段，与现有 `reasoning_content` 同例**，普通客户端会忽略）：
```json
{ "choices": [{ "message": {
    "content": "已重新生成…\n\n[🧩 交互组件](http://host/admin/kimi/widgets/ab12.html)",
    "widgets": [{ "id": "ab12", "html": ".../ab12.html", "png": ".../ab12.png", "bytes": 28292 }]
}}]}
```
- `content` 里仍保留链接（对纯 OpenAI 客户端可用）；
- 需要"图片化"的客户端可选 `options.widget_png: true` → 追加 markdown 图片 `![](…ab12.png)`；
  **默认不开**，避免响应体膨胀（base64 内联会更夸张，不提供）。

## 3. Playground 呈现

消息下方新增「**组件**」面板（与正文同级，默认展开）：

```
┌ 组件 ──────────────────────────── [交互|截图] [新窗口打开] [复制 HTML] ┐
│ <iframe srcdoc="<HTML>" sandbox="allow-scripts"></iframe>              │
└───────────────────────────────────────────────────────────────────────┘
```
- **交互**：`srcdoc` + `sandbox="allow-scripts"`（**不给 `allow-same-origin`** → 组件运行在不透明源，
  拿不到我们的 cookie/localStorage、也碰不到我们的 DOM）；
- **截图**：切到 `<img src="/admin/{p}/widgets/{id}.png">`（HTML 渲染异常时的保真兜底）；
- 「新窗口打开」= `/admin/{p}/widgets/{id}.html`（**⚠ 直接打开会在同源下执行组件脚本**，需加
  `sandbox` 或提示风险 —— 实现时改为"下载"或加 `Content-Security-Policy: sandbox`）；
- 历史会话：切换 thread 时按消息里的 `widgets` 一起回填。

## 4. 安全

| 风险 | 处置 |
|---|---|
| 组件脚本越权（读我们 UI 的 DOM/cookie） | iframe `sandbox="allow-scripts"`（不透明源）；**绝不**加 `allow-same-origin` |
| 组件里含外链、运行时请求 | 实测自包含；实现时对 HTML 做**大小上限**（512KB）+ 可选 `<base>`/CSP 限制 |
| 直接打开 `.html` 被当作同源页面 | 响应头加 `Content-Security-Policy: sandbox allow-scripts; default-src 'none'` 或强制下载 |
| 路径穿越 | id 正则 `^[A-Za-z0-9_-]{1,64}$`，文件名由服务端生成（不拼接用户输入） |
| 组件过多导致磁盘膨胀 | 每线程最多保留 N 个（如 10），超出删最旧；随 thread 删除 |
| 抓取行为本身 | 仍是"读渲染结果的 HTML/像素"，**不是**逆向内部 API，符合 `docs/DESIGN.md` 原则 |

## 5. 配置

```yaml
kimi:
  selectors:
    include_frames: true        # 已有：正文里附「链接 + 可见文本」兜底
    widget_capture: html        # none（默认）| png | html | both
```
- 只有开启 `widget_capture` 的 provider 才做存储/接口（通用框架不受影响）；
- `both` 时 Playground 默认显示 HTML、可切截图。

## 6. 分期（每步独立可验证）

| # | 提交 | 内容 |
|---|---|---|
| C1 | `feat(widget): capture iframe html/png per turn` | 新 frame 识别 → 落盘 `profiles/<p>/widgets/` → 元数据入库 + 单测（cap 大小、id 校验、清理） |
| C2 | `api: /admin/{p}/widgets/{id}.{html,png}` + messages 带 widgets | 路由 + CSP/沙箱响应头 + 测试 |
| C3 | `ui(playground): widget panel (interactive iframe + screenshot toggle)` | 面板/切换/新窗口/历史回填 + Playwright 验证 |
| C4 | （可选）`options.widget_png` | 让 API 客户端也能拿到图片（默认关） |

## 7. 成本与风险

- 规模：C1+C2 ≈ 1 天，C3 ≈ 半天（含测试）。
- 主要风险：① 组件的 HTML 是"从渲染结果里抓的"，**含内联脚本**，必须靠 sandbox 隔离；
  ② 部分组件可能依赖运行时/外链（本次实测没有，但不能假设）→ PNG 兜底；
  ③ 存储与消息关联需要一次 DB 迁移（`ADD COLUMN`，项目已有 `attachments` 迁移先例）。

## 8. 待确认

1. **产物**：只做 HTML（可交互）？只做 PNG（保真）？还是 both（推荐，默认 HTML + 可切截图）？
2. **API 侧**：`widgets` 非标准字段 + content 里保留链接 —— 可以吗？（默认不内联 base64）
3. **存储**：落盘 `profiles/<provider>/widgets/` + `messages` 加一列元数据 —— 可以吗？
4. **Playground 默认**：交互 iframe 展开、截图折叠；历史会话一起回填 —— 可以吗？
5. 是否也支持**其他 provider**（ChatGPT 的 canvas/图表、DeepSeek 的图片）—— 通用能力，还是先只做 Kimi？
