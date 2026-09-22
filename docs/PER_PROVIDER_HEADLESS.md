# 按 provider 区分 headless（**暂缓**，设计存档）

> 状态：**暂缓（deferred）**。2026-09 决策：Docker 下已是"无打扰"（一个共享 headful 浏览器跑在 Xvfb 里，看不见窗口），
> 收益只落在**本地开发体验**上，不值得现在重构 `BrowserManager` 生命周期。触发条件见文末。

## 1. 现状事实（本次调研结论）

- `BrowserManager` 是**一个 Chromium 进程**（`self._browser`）+ **每个 provider 一个 BrowserContext**
  （`self._contexts[provider]`）→ cookie/localStorage 隔离，进程共享。
- `headless` 是 `chromium.launch()` 的**进程级**参数 → 共享浏览器时**无法**按 provider 区分。
- 例外：`browser.status_check_headless: true` 时，定时登录检测会**另起一个临时 headless 浏览器**（每轮启停）。
- **陷阱**：`_apply_headless_override` 里 `<PROVIDER>_HEADLESS` 命中即覆盖**全局**
  `browser.headless` → 设 `CHATGPT_HEADLESS=false` 会把 deepseek 也一起变 headful（名不副实）。
- 实测（容器，全局 headful + Xvfb）：容器内存 **266 MiB**，chrome 主进程 RSS ~295 MB，Xvfb RSS 64 MB。

## 2. 为什么不划算（针对"Docker 也分组"）

按 headless 分组 = 启动 **2 个 Chrome 进程**：省下的是 DeepSeek 那点渲染开销（几十 MB），
多花的是**多一个 Chrome 进程**（~150–300 MB），且 **Xvfb 省不掉**（chatgpt 需要它）→ 净收益为负。

## 3. 目标场景（本次讨论确认）

用户诉求：**本地运行按需**（只开必要的浏览器、最大程度减少打扰），**Docker 保持现状**。
→ 此时收益成立：本地只用 deepseek 时**一个窗口都不弹**；只有真的用到 chatgpt 才开 headful。

## 4. 若要做：三个必须同时满足的点

1. **懒加载分组**（最关键）：`_browser` → `_browsers: dict[bool, Browser]`，按 provider 的 headless 取/建；
   启动时只拉**全局默认组**，headful 组**只在真正用到该 provider 时**启动。否则启动即弹窗，懒加载失效。
2. **后台检测不得拉起未启动的组**：`refresh_login_status → check_login → get_context` 会拉起浏览器；
   规则改为「**只检测浏览器组已启动的 provider**」，未启动的沿用 state.json 种子状态。
   Docker 下全局组启动即在 → 行为不变。
3. **开关从 `.env` 挪到 `docker-compose.yml`**：`.env` 会被 `load_dotenv` 读到 → 本地也会被强制 headful。
   改由 compose 设 `WEB2API_HEADLESS: "${WEB2API_HEADLESS:-false}"`，`.env` 删除该项。

优先级（顺带修掉 §1 的陷阱）：
`<PROVIDER>_HEADLESS` > `providers.<p>.headless` > `WEB2API_HEADLESS` > `browser.headless`

## 5. 更好的抽象（用户建议，推荐后续采用）

不要用布尔开关，而是**能力声明**：

```yaml
providers:
  chatgpt:
    headless: false          # 或：
    # headless: "auto"       # auto = 由能力决定
    requires_headful: true   # 能力：Sentinel 拦截 headless → 必须 headful
  deepseek:
    requires_headful: false  # headless 可选
```

- `requires_headful: true` → 该 provider 强制 headful（不接受 headless）；
- 其余 provider 交给 `browser.headless` / 全局开关；
- 有了能力声明，系统可**自动决策**（"只要有启用中的 `requires_headful` provider，就必须准备 Xvfb/headful 组"），
  比手工布尔开关更灵活，也便于把"为什么必须 headful"这个知识固化在配置里（自文档化）。

## 6. 预期效果（若实施）

| | 本地 | Docker |
|---|---|---|
| deepseek/qwen | headless（共享 1 进程，无窗口） | headful（Xvfb，不可见） |
| chatgpt | headful（**用到才开窗**） | headful（同上） |
| Chrome 进程数 | 1（只用 deepseek）→ 2（用到 chatgpt） | **1（与现状完全一致）** |

## 7. 触发条件（何时值得做）

- 本地频繁使用 chatgpt，且 headful 窗口造成实际打扰；
- 或接入更多"必须 headful"的站点，需要系统的统一能力声明；
- 或需要为不同 provider 指定不同浏览器参数（proxy/UA/时区）——同一套分组机制可以顺带支持。

---
*相关：`docs/DESIGN.md`（浏览器与登录态模型）、`src/ai_web2api/browser/manager.py`、`src/ai_web2api/config.py:_apply_headless_override`*
