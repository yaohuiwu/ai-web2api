# 豆包（doubao.com）接入说明

> 状态：**可用**（`enabled: true`）。驱动：`src/ai_web2api/providers/doubao.py`。
> 登录：**仅手动**（无密码登录：手机号验证码/扫码）。

## 1. 实测结论（2026-09，已端到端验证）

| 项 | 结果 |
|---|---|
| 站点 | `https://www.doubao.com/chat`（会话页 `/chat/<数字 id>`） |
| 输入框 | `div.tiptap.ProseMirror[contenteditable=true]`（**tiptap/ProseMirror，无 textarea**）→ `type_prompt: true` |
| 发送控件 | **`.send-btn-wrapper`**（实测是 **DIV 不是 button**，输入后才出现）→ `send_button` |
| **正文容器** | `[data-container-type="block-v2"] > div:not([class*="justify-end"]) .md-box-root` |
| 为什么不能直接用 `.md-box-root` | 它**同时匹配用户提问与助手回答**（用户侧在 `.flex.justify-end` 里）→ 会抓到"提问"；上面这条只匹配助手侧（实测 count=2，末条就是最新回答） |
| 登录判定 | **反向标记** `selectors.logged_out: ['[class*="login-btn"]', 'button:has-text("登录")']` —— 豆包**游客态也有输入框**，只靠 input 会把游客误判成已登录（手动登录命令会直接退出） |
| 区域限制 | **容器内未登录**会提示「受区域限制，请先登录再使用豆包」 |
| 会话 URL | `/chat/<数字>` → `session_url_pattern = r"/chat/(\d{6,})"`，thread 恢复已验证（`url_id` 已落库） |
| 深度思考折叠区 | ⚠ 未观测到（本阶段回复都没有）→ `thinking_container: []`；若出现思考文案，按 `docs/PROVIDER_KIMI.md` 的做法收窄选择器 |

### 两个必要的修正（踩过的坑）

1. **一次回答会被拆成多个 `.md-box-root`** → 只取第一个会**截断**（实测表格用例只拿到表头+尾巴碎片，覆盖率 0.08）。
   配 `selectors.response_all_new: true`：把检测到的新容器**全部拼接** → 覆盖率回到 **1.00**。
2. **表格/代码类回答重渲染频繁** → 配 `selectors.stream_content: false`（缓冲后一次发），避免逐字 diff 拿到中间态残片。

## 2. 端到端验证

```
POST /v1/chat/completions {"model":"doubao-web","thread_id":"doubao-cal-1", ...}
→ content='我是豆包，由字节跳动基于 Seed 大模型基座独立研发的 AI 助手。'   14.7s
文本保真度回归（tests/test_text_fidelity.py -k doubao）：3/3 通过（长文本 / 代码块 / 表格，覆盖率 1.00、无思考污染）
```

## 3. 登录（手动，一次即可）

```bash
./scripts/login.sh doubao      # 会弹出浏览器：扫码/验证码登录 → 自动导入服务
curl http://127.0.0.1:8000/admin/doubao/login/status   # 确认 logged_in: true
```
> 未登录时服务侧会**如实**报告 `logged_in: false`（靠反向标记），不会假阳。

## 4. 如果站点改版

```bash
curl -X POST http://127.0.0.1:8000/admin/doubao/debug/probe \
  -H 'Content-Type: application/json' -d '{"message": "只回复两个字：收到"}'
curl 'http://127.0.0.1:8000/admin/doubao/debug/dom?selector=.md-box-root'   # 看容器结构是否变化
```
改完 `config.yaml` 只需 `docker compose restart`（compose 已 bind mount 配置，**不必 rebuild**）。

## 延迟拆解（实测，2026-09-22）

用户反馈："画面上早就生成完了，Playground 还不出字"。实测拆解（同一条请求）：

| 段 | 耗时 | 结论 |
|---|---|---|
| 恢复页面 → 发送前 | ≈4s | 与豆包无关（可选 `provider.restore_timeout` 调优） |
| DOM 抓取本身 | **16ms/拍**（最大 390ms） | **不是抓取的锅** |
| 发送 → 正文停住 | 生成时间（本例 ~10–16s） | 站点侧 |
| **正文停住 → 我们定稿** | **≈8s 白等** | **元凶**：`stable_polls=12`，正文出现后放宽到 `12×3=36` 拍 |

**为什么"很早生成完却检测不到"**：豆包**没有可用的停止按钮**（实测候选选择器全空；
`#to-bottom-button`/`dot-flashing` 语义与滚动/思考有关，不可靠）→ 只能靠"文本不变"判定；
加上 `stream_content: false`（缓冲）→ 定稿前**一个字都不发**，所以画面页有字、API 空白。
修复前实测：**整段正文在 `+29.18s` 作为唯一 1 条增量一次性到达**。

**修复：预览流**（`preview_stream: true` + `preview_min_chars: 12`）——把"显示"与"收尾"解耦：

- **一有新增就发**（只发**前缀追加**，非前缀改写丢弃，防脏数据）；
  ⚠️ 不能要求"稳定 N 拍后才发"：站点连续吐字时每拍都在变，等稳定＝等到停顿才发（实测就是这个 bug）；
- 硬定稿条件**完全不动**（`36` 拍安全网照旧，防"中途停顿 8.5s 后继续写"被截断——实测豆包会出现这种停顿，且与旧窗口几乎等长）；
- 定稿时按"最长公共前缀"补发尾巴 → 拼接结果与最终正文完全一致（保真度 3/3，覆盖率 1.00）。

效果：首字 `+29.2s → +8.2s`；增量间隔 **0.20–0.22s**（真流式，此前是结尾一次性返回）。
注意：**流关闭时刻不变**（客户端若"等流结束"仍会等到安全定稿点）。


## 结束信号：**没有任何可用的站点信号**（2026-09-22 实测）

为了削掉 `settle_lag=12.7s`（时间线：正文 3.7s 就不再变，我们 16.4s 才定稿），逐个验证了候选信号：

| 候选 | 实测结果 |
|---|---|
| 停止按钮 | ❌ 不存在（`button[aria-label*="停止"]`、`[class*="stop-btn"]` 全空） |
| 助手消息工具栏（复制/点赞/朗读…） | ❌ **不存在**：hover 到回答上前后节点数 `7→7`、`button` 数 `0→0`；唯一 `[class*="message-action-bar"]` 属于**用户消息**（`justify-end` 链）且默认隐藏 |
| `#to-bottom-button`（含 `loading-i3Fu5w`） | ⚠️ 生成中可见、结束消失，但语义跟随滚动，不可靠 |
| `dot-flashing`（三点动画） | ⚠️ 只在"思考/加载"阶段出现，正文开始后即消失 → 不能当结束信号 |

**结论**：豆包的快速结束信号只能来自**网络侧**（"完成请求的响应流结束"事件，路线 2）或继续用稳定性兜底（当前 36 拍）。

