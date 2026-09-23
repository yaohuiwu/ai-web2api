# 画面交互（点击 / 拖拽 / 输入）设计 — 方案对比与选型

> 状态：**设计待评审**（未实现）。开关沿用 `server.live_control`（**默认 false**）。
> 前置：画面改为**整页**（已改）；缩放控件保留（适应/100%/150%/200%）。

## 1. 目标 / 非目标

**目标**
1. 在 Playground 右侧画面上直接：**点击**、**拖拽**（含按住→移动→松开）、**滚轮**、**键盘输入/快捷键**。
2. 覆盖真实用途：过站点弹窗（繁忙提示/协议）、点按钮、在输入框里打字、**拖拽过图形验证码**（真人操作，不自动解题）。
3. 与现有只读直播**共存**：不开启控制时行为完全不变（零回归）。
4. 实现尽量小：**不引入新进程/新协议**。

**非目标**
- 不自动识别/破解验证码（只提供"人能操作"的通道）。
- 不做多用户/权限体系（沿用本项目的"本机/内网 + 可选 API Key"边界）。
- 不做"操作录制/回放"。

## 2. 候选方案对比

| 方案 | 做法 | 优点 | 缺点 | 结论 |
|---|---|---|---|---|
| **A. 坐标映射 + Playwright 输入**（推荐） | 前端把点击位置按比例映射成页面 **CSS 坐标** → `POST /admin/{p}/input` → 后端用 `page.mouse.move/down/up`、`page.keyboard.*`、`page.mouse.wheel` 派发 | **不引入新进程/协议**；事件是**真实输入**（isTrusted=true）→ 框架与验证码都认；拖拽/滚轮/键盘都能做；实现量小（1 个接口 + ~80 行前端）；与现有 MJPEG/状态接口天然配套 | 点的是**可能过期 ≤1 帧**（≈200ms）的画面；需要正确的坐标换算（见 §4）；被"图片缩放/letterbox"影响，需要按**显示框**换算 | ✅ **选它** |
| **B. noVNC（Xvfb + websockify + VNC 客户端）** | 额外容器暴露真实桌面，用 iframe 嵌 VNC | 交互**完全原生**（含 IME、文件拖放、右键菜单）；不依赖我们的映射 | 多一套**重进程/端口/运维**；iframe 内是第二套 UI，与 Playground 脱节；**程序侧（API/Agent）用不到**；安全面更大（VNC 端口） | ❌ 过重（仅当将来要"完整桌面级手动操作"再考虑） |
| **C. 页面内 JS 合成事件**（`elementFromPoint(x,y).click()` / `dispatchEvent(new MouseEvent(...))`） | 注入 JS 按坐标/元素派发合成事件 | 不需要后端输入接口；按**元素**点击，不依赖像素精度 | 合成事件 `isTrusted=false`：**验证码/风控会拒**，部分 React 组件也不响应；拖拽要手写 pointer 序列；与"不注入业务逻辑"的红线擦边 | ❌ 不满足"能过验证码/点得动" |
| **D. 客户端直连 CDP**（把 CDP WebSocket 暴露给浏览器） | UI 直接发 `Input.dispatchMouseEvent` | 无后端转发、延迟最低 | 等于**把浏览器全权暴露给任何能访问页面的人**；还要处理 CDP 会话/多页路由；安全与复杂度都不划算 | ❌ 安全不可接受 |
| **E. 仅键盘 / 仅预设按钮**（不实现任意坐标） | 只提供"Enter/Esc/Tab/方向键 + 刷新/回底" | 最简单、最安全 | 不能点按钮、不能拖拽 → 覆盖不了验证码这类核心场景 | ❌ 能力不足 |

**选型：A（坐标映射 + Playwright 输入）**——"最好且最简单"：复用现有 HTTP/直播栈，输入是真实事件（验证码/组件都认），拖拽与键盘一并解决。

## 3. 架构（方案 A）

```
前端（Playground 右栏）
  <img id=live>  ←─ MJPEG（整页，viewport 截图）
    │  mousedown/mousemove/mouseup/wheel/keydown
    │  按"显示框"比例换算 → 页面 CSS 坐标
    ▼
POST /admin/{provider}/input   {action, x, y, ...}
    ▼
后端（live_control 开关校验 + 串行闸门）
  page.mouse.move/down/up | page.mouse.wheel | page.keyboard.type/press
    ▼
真实输入事件（isTrusted=true）→ 站点响应
（MJPEG 下一帧自然反映结果，无需前端手动刷新）
```

## 4. 坐标换算（唯一需要精确的地方）

- 画面帧 = **viewport 截图**（`page.screenshot()` 无 clip、非整页滚动），尺寸 = CSS 视口 × `device_scale_factor`。
- 前端换算**用比例，天然与 dsf 无关**：
  ```
  cssX = (clientX - rect.left) / rect.width  * viewportW
  cssY = (clientY - rect.top)  / rect.height * viewportH
  ```
  其中 `rect` 必须是**图片实际渲染框**（`<img>` 的 `getBoundingClientRect()`），`viewportW/H` 取自
  `GET /admin/{p}/screen/state`（已返回 `viewport`）。
- **letterbox 处理**：若用 `object-fit: contain`，rect 含黑边 → 先按宽高比算出真实图片框再换算。
  **交互模式的简化决定**：img 用 `width:100%; height:auto`（无 letterbox），超出部分由**面板滚动**查看 ⇒ rect 恒等于图片框。
- 坐标**夹取**到 `[0, viewport)`；越界不算错误（用户按在边缘）。
- 页面滚动**不影响**映射（截图就是当前滚动状态，Playwright 的鼠标坐标也是视口坐标）。

## 5. 接口设计

```
POST /admin/{provider}/input
{ "action": "click" | "move" | "down" | "up" | "drag" | "wheel" | "type" | "key" | "reload" | "to_bottom",
  "x": 640, "y": 320,              # CSS 像素（click/move/down/up/drag/wheel 需要）
  "x2": 900, "y2": 320,            # drag 终点
  "text": "你好",                   # type
  "key": "Enter",                   # key（Enter/Escape/Tab/ArrowDown/Backspace…）
  "dy": 600, "dx": 0,               # wheel
  "steps": 12, "delay_ms": 25,      # drag 插值（默认 12 步 × 25ms）
  "button": "left",                 # 预留（默认左键）
  "thread_id": "..."                # 可选：指定会话页面
}
→ 200 {"ok": true, "action": "click", "mapped": {"x": 640, "y": 320}, "viewport": {...}, "frame_t": 123.4}
```

- `click` = `move → down → up`（3 次真实事件）。
- `drag` = `move(起点) → down → N 次 move(插值) → up`（**一次请求完成**，避免 HTTP 往返把拖拽打散——
  这正是图形验证码拖拽成功的关键）。
- `reload` = `page.reload()`；`to_bottom` = 滚到底（End 键或 wheel 大值）。
- 返回里带 `viewport` 与 `frame_t`（前端可用它显示"画面帧龄"，提示用户画面可能滞后）。

## 6. 前端交互（最小可用）

| 操作 | 行为 |
|---|---|
| 单击 | `click`（在画面上按下即发；`move` 不必每次发，避免刷接口） |
| 拖拽 | `mousedown` 起、画面上跟随显示一条**虚线引导**、`mouseup` 发一次 `drag`（起点/终点） |
| 滚轮 | 默认**滚动面板**（看画面其他部分）；按住 **Shift** → `wheel` 发给**页面** |
| 打字 | 面板下方一个输入框 + 「输入」按钮（Enter 直接发）；另提供 Enter/Esc/Tab/↑↓←→/Backspace 快捷键按钮 |
| 其它 | 「刷新页面」「回到底部」 |
| 只读保护 | 顶部「🖱 交互」开关（默认**关**）；关闭时画面纯只读，所有输入事件不拦截 |

## 7. 安全与并发

- **开关**：`server.live_control: false`（默认）→ `/input` 一律 **403**（前端自动隐藏交互 UI）。
- **可选鉴权**：若配置了 `WEB2API_API_KEY`，`/input` 也要求 `Authorization: Bearer …`（与 `/v1` 一致）。
- **并发**：同一 provider 的输入**串行化**（进程内 `asyncio.Lock`），并与"正在生成"互不阻塞（生成中允许操作，比如点"停止"）。
- **审计**：每个 `action` 记一条 INFO 日志（含 provider/action/坐标/耗时），沿用 `[timeline]` 的风格便于 grep。
- **不做**：不提供任意 JS 执行接口（避免变成"远程代码执行"）。

## 7.5 拖拽会不会影响后续点击？（已实现部分的实测 + 后续防护）

**结论：会，但都能防住。** 分两类：

### A. 中缝拖拽（已实现，已实测）
风险：在 `window` 上挂的 `pointermove/pointerup` 若**没有被摘掉**（典型场景：鼠标在窗口外松开、
窗口失焦、触屏手势被系统打断）→ 之后的普通移动会被当成"继续拖拽"，用户表现为"点击失灵/界面乱动"。

防护（全部已实现并实测）：
| 措施 | 作用 |
|---|---|
| `handle.setPointerCapture(pointerId)` | 保证一定能收到 `pointerup`（即使指针移出元素） |
| `pointerup` / `pointercancel` / `blur` 三处收尾 | 任何异常路径都能结束拖拽 |
| `pointermove` 里检查 `e.buttons === 0` | 鼠标已在别处松开 → 立即结束 |
| 结束必 `removeEventListener` | 不留 window 级监听 |
| 拖拽时 `preventDefault()` | 不误选文本 |
| 双击复位 / ←→ 键微调 / 宽度记忆 | 不用精确拖动也能调 |

实测结果：拖拽 `500 → 620px`；**松开后再移动鼠标宽度不再变化**；**随后点击输入框正常聚焦**；
模拟"窗口外松开"也不会卡住（`dragging=false`）。

### B. 画面上的点击/拖拽（P1/P2 待实现）
| 风险 | 防护 |
|---|---|
| **点击 vs 拖拽含糊**：手抖 2px 就被判成拖拽 → 站点变成"选文本"而不是点按钮 | 设**位移阈值**（≤4px 当点击，否则当拖拽）；拖拽时画面显示虚线引导 |
| **服务端漏发 `mouseup`** → 站点卡在"拖拽中"，后续点击全乱 | `drag` 在 `finally` 里**一定发 `up`**；前端 `setPointerCapture` 保证能收到 `pointerup`；提供 **「重置输入」按钮**（发 `mouse.up()` + `Esc`） |
| **滚轮语义冲突**：面板滚动 vs 页面滚动 | 默认滚**面板**；`Shift+滚轮` 才发给**页面**（文档化，避免误操作站点） |
| **hover 才出现的控件** | 点击前先发 `move`（Playwright `mouse.click` = move+down+up），保证 hover 态先成立 |
| **生成中点击被站点忽略**（如点"停止"） | 面板头部显示"生成中"；输入**串行化**（`asyncio.Lock`），不会交错成坏序列 |
| **坐标过期（≤1 帧 ≈200ms）**：画面是旧状态 → 点偏 | 状态栏显示**帧龄**；动作本身会触发重绘，下一帧很快纠正 |
| 面板自身滚动被误当页面滚动 | 我们只换算**图片像素**；页面滚动状态与截图一致，不影响映射 |

## 8. 测试计划

**单元（无浏览器）**
1. 坐标换算纯函数：比例/夹取/letterbox（含 0 宽高、负偏移等边界）；
2. `action` 解析与参数校验（缺 x/y → 400；未知 action → 400）；
3. `live_control=false` → 403（且前端测试断言 UI 隐藏逻辑存在）；
4. 拖拽插值：`steps/delay` 生成的点序列单调、首尾等于起终点。

**集成（fake page）**
5. `click` → 断言调用序列 `move/down/up` 且坐标正确；
6. `drag` → 断言 `move/down/move×N/up`（N=steps，首尾正确）；
7. 串行闸门：并发两个 `/input` → 不交叉（用假 mouse 记录时间戳）。

**Live（真站点）**
8. 在 `tests/fake_chat.html`（本地假站点，可控）上端到端：点击输入框 → 输入文字 → 点击发送 → 断言假站点收到了期望事件；
9. 真站点冒烟：DeepSeek 点输入框 + 输入 + Enter → 能看到回复（题目从文本库随机取）；
10. 拖拽冒烟：假站点上拖拽一个元素 → 断言 `mousemove` 序列与落点正确。

## 9. 分期

| 期 | 内容 | 价值 |
|---|---|---|
| **P1** | `click` / `wheel` / `type` / `key` / `reload` / `to_bottom` + 交互开关 + **「重置输入」**（发 `up`+`Esc`，解卡）+ 面板滚动/缩放 | 覆盖"点按钮、打字、提交"，且不会被拖拽脏状态带偏 |
| **P2** | `drag`（一次请求内插值）+ 前端虚线引导 | **过图形验证码**、拖拽排序 |
| **P3**（可选） | 文件上传（`set_input_files`）/ 粘贴图片、右键菜单 | 与附件能力对齐 |

**每期都跑**：快速套件 + `tests/fake_chat.html` 集成 + 真站点冒烟；不动只读路径。
