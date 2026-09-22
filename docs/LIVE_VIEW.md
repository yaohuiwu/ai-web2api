# 实时浏览器画面（Live View / Remote Control）— 设计，待 review

> 状态：**待 review**（未实现）。目标：让 UI 能**实时看到**指定 provider 的浏览器画面，
> 并（可选）**直接操作它** —— 最主要的价值是把「手动登录（验证码/扫码/OAuth）」从宿主 CLI 搬进 UI。

## 1. 场景与非目标

**场景**
- **A（主）在 UI 里完成手动登录**：点「开始登录」→ 直播看到登录页 → 在 UI 里输手机号/点验证码/扫码 → 服务自动保存登录态。**不再需要**宿主跑 `login.sh` + 导 state。
- **B 观测/调试**：自动化卡住时看页面到底停在哪（选择器失配、风控页、loading 遮罩）。
- **C（可选）遥控台**：点击/输入/滚动/切标签，当"网页版 Playwright 遥控器"。

**非目标**：替代 Playwright 自动化；多人协作/共享控制；录屏归档。

## 2. 现有基础

- 一次性截图已有：`POST /admin/{p}/login/screenshot`（写盘 `login_error.png`）+ `GET`（回 PNG）
- 调试已有：`GET /admin/{p}/debug/dom?selector=`、`POST /admin/{p}/debug/probe`
- 浏览器模型：**单实例多 context**（每 provider 一个 context，独立登录态）；Docker 里 headful 走 Xvfb，headless 也能 `page.screenshot`
- 前端：3 个静态页 + `common.js`（已封装 `api()`/`toast()`/`copyText()`），**尚无 WebSocket 使用**

## 3. 性能实测（决定帧率与画质）

真实页面（kimi.com，1440×900，headless，8 次取中位）：

| 配置 | 单帧耗时 | 帧大小 | 折算 |
|---|---|---|---|
| JPEG q=80（默认） | 16.6 ms | 44 KB | 5 fps → 8% 单核 / 220 KB/s |
| **JPEG q=50** | **16.5 ms** | **31 KB** | **5 fps → 8% 单核 / 155 KB/s** |
| 720×450 JPEG q=50 | 16.6 ms | **9 KB** | 10 fps → 16% 单核 / 90 KB/s |

**结论**：耗时几乎与画质/尺寸无关（约 16ms 是 CDP 往返 + 编码的固定成本），
**真正的成本是带宽与 CPU 占用率**，不是单帧延迟 → 简单的 `page.screenshot` 循环（5–10 fps）就够用，
**不必**引入 CDP `Page.startScreencast`（可作为后续优化，收益是帧 diff 降 CPU）。

## 4. 方案对比

| 方案 | 帧率 | CPU | 可控制 | 能看到弹窗/新标签 | 平台 |
|---|---|---|---|---|---|
| **A 轮询截图**（前端定时换 `<img>`） | ≤2 fps | 低 | ✗ | ✗ | 全平台，改动最小（P0） |
| **B MJPEG 推流**（服务端循环 + `multipart/x-mixed-replace`） | 5–15 fps | 中 | ✗ | ✗ | 全平台（**推荐 P1**） |
| C CDP screencast | 15–30 fps | 低 | 需另做输入注入 | ✗ | Chromium |
| D X11 抓屏（容器内 `ffmpeg -f x11grab :99`） | 15–30 fps | 中 | ✗（要 xdotool） | **✓ 整个窗口（含 OAuth/扫码弹窗）** | 仅 Docker/Linux + Xvfb（P3） |
| E noVNC（x11vnc + websockify） | 15–30 fps | 中 | ✓ | ✓ | 需额外进程/端口，与"纯 Python 服务"风格不符（不建议） |

**推荐路线**：**P0 轮询 → P1 MJPEG → P2 输入注入 → P3（可选）X11 抓屏**。

## 5. 详细设计

### 5.1 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/{p}/screen.jpg?quality=50&max_width=1280&clip=…` | **单帧** JPEG（`Cache-Control: no-store`）。P0 前端轮询它即可"近乎实时"，零新依赖 |
| GET | `/admin/{p}/stream.mjpg?fps=5&quality=50&max_width=1280` | **MJPEG 流**（`multipart/x-mixed-replace; boundary=frame`），`<img src>` 原生支持，无需 JS 解析（P1） |
| GET | `/admin/{p}/screen/state` | `{available, streaming, fps, viewers, page_url, viewport, active_request, error?}` |
| WS | `/admin/{p}/live` | 双向：帧下行 + 输入上行（P2；有输入需求时才需要） |
| POST | `/admin/{p}/screen/input` | 备选（无 WS 也能控制）：`{type:"click|move|wheel|key|text", …}`（P2） |

> 先做 A/B 两个只读接口就能覆盖 90% 价值（观测 + 看登录页）。

### 5.2 服务端

- 新模块 `src/ai_web2api/browser/live.py`：
  - **每 provider 一个采集任务**，多观众**扇出**同一帧（不重复截图）；**最后一个观众离开即停**（+ 5s 宽限），无人观看时零开销。
  - 帧参数：默认 `fps=5`、`quality=50`、`max_width=1280`（超出等比缩放）；`clip` 可选（例如只看 `.chat-content-item-assistant`）。
  - **取页策略**：优先该 provider **正在被自动化使用的那张 page**（`ThreadSession.page` / `login` 页），否则 `context.pages[-1]`；**没有 context 时默认不创建**（避免"看一眼"就把浏览器拉起来）。
  - **与自动化互斥**：截图是只读，但会占 CPU/CDP → 检测到该 provider 有活跃请求时**自动降帧**（5→1 fps）并在 state 里标注，避免拖慢生成。提供 `?fps=0`（暂停）。
  - 失败处理：页面崩/关 → 推一帧占位（提示原因）并结束循环；`state` 里带 `error`。
- 沿用现有 `BrowserManager`（`get_context` 已是异步+懒加载），**不改**浏览器模型。

### 5.3 前端

- **新页面 `/ui/browser.html`**（"实时画面"）——与状态面板解耦（沿用 threads 页的做法）：
  - 顶部：provider 切换（数据来自 `/admin/status`）、`page_url`、视口尺寸、FPS/画质下拉、暂停/继续、「保存当前帧」。
  - 主体：`<img id="live" src="/admin/{p}/stream.mjpg?fps=5">`（P1）/ 定时轮询（P0）。
  - P2：叠加坐标层 —— 点击 → `POST /screen/input {type:click,x,y}`（按显示尺寸→视口尺寸换算）；键盘输入（含中文 `insert_text`）；滚动；**红色"控制中"标识 + 一键释放**。
- 状态面板 provider 详情：加「**查看画面 →**」按钮，深链 `/ui/browser.html?provider=kimi`。
- 手动登录流程（场景 A）：详情页「开始登录」→ 打开该 provider 的登录页 → 自动跳到画面页 → 用户操作 → 服务端 `check_login()` 通过后落盘 + 提示"已保存登录态"。

### 5.4 安全（必须先定）

⚠️ `/admin` **目前不鉴权** —— 直播 = 把你**已登录**的浏览器暴露给任何能访问该端口的人；加上输入注入就等于把账号交出去。因此：

- **默认关闭**：`admin.live_view: false`；开启时**必须**配置 token（复用 `WEB2API_API_KEY`，或新增 `admin.live_token`），校验失败 403。
- **控制与观看分离**：`admin.live_control: false` 默认关（先只读）；开启需显式确认 + 每次会话 60s 无输入自动断开。
- 文档与 UI 都要有醒目警告：对外暴露必须加反代鉴权；建议只在 `127.0.0.1` 或 Tailscale 内网使用。
- 审计：开启/停止观看、每次输入都记日志（provider、来源 IP、动作类型）。

### 5.5 测试

- 单测（不依赖浏览器）：帧参数解析与缩放计算；采集循环**多观众只截一次**、无观众自动停；
  `live_view=false` → 403；无可用 page → 降级（占位/`available:false`）。
- 用 stub page 假 `screenshot()`（返回 1×1 JPEG bytes）验证 MJPEG 分帧格式（boundary/headers）。
- 集成（本地）：`GET screen.jpg` → JPEG magic + 尺寸在允许范围；`stream.mjpg` 3 秒内 ≥2 帧。
- Playwright：打开 `/ui/browser.html` 帧数增长；暂停后不再增长；`?provider=` 深链生效。

## 6. 工作量与风险

| 阶段 | 内容 | 规模 |
|---|---|---|
| **P0** | `screen.jpg` 单帧 + `/ui/browser.html` 骨架（轮询 1–2 fps）+ 「查看画面」入口 | 小（~半天） |
| **P1** | `stream.mjpg` 采集循环（扇出/自动停/降帧/state） | 中（~1 天） |
| **P2** | 输入注入（点击/键盘/滚动）+ 坐标换算 + 安全开关 + 审计 | 中偏大（~1–2 天） |
| **P3** | X11 抓屏（看整窗/弹窗；容器加 `ffmpeg`） | 中（~1 天，仅 Docker/Linux） |

**主要风险**：① 截图抢占 CPU 拖慢生成（用降帧 + 默认 5 fps 缓解）；② `/admin` 无鉴权下的安全（必须 P0 就把 token 开关做进去，不能后补）；③ 多观众放大开销（扇出解决）。

## 7. 待确认（拍板后开工）

1. **先做哪一档**？建议 **P0 + P1（纯只读）**，先把"看得见"做出来。
2. 要不要**控制**（输入注入）？—— 若目标是"UI 里完成手动登录"，**必须要**（否则看得到验证码也点不了）→ 那 P0 里就该把安全开关一起做。
3. 要不要看**弹窗/新标签**？—— 决定是否做 P3/X11（微信扫码、Google OAuth 弹窗属于这一类）。
4. 安全基线选哪个：**默认关闭 + 必须配 token**（推荐）？还是**只允许 127.0.0.1 访问直播接口**？
5. 默认参数：**5 fps / JPEG q=50 / 最宽 1280**、暂停按钮、生成中自动降到 1 fps —— 可以吗？
