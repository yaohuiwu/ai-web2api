# 会话（Thread）独立页面（设计，待 review）

> 状态：**待 review**（未实现）。
> 目标：把「会话浏览」从状态面板拆到独立页面 `/ui/threads.html` —— 展示标题、按时间倒序、**加载更多**、**文本搜索**。
> 约束：只新增只读查询能力 + 静态资产；**不改** OpenAI 兼容行为，`/admin/threads` 保持向后兼容。

## 1. 现状与问题

- 状态面板里混了两类东西：**运维状态**（登录态/认证有效期/服务参数）和**会话浏览**（表格）。
- `/admin/threads` → `ThreadManager.list()` 一次返回**全部**（内存活跃 + SQLite 持久化合并，按 `updated_at` 倒序）。
  当前实测 **36 条**，越多越难受：
  - 状态页每次刷新都渲染大表（与运维信息抢注意力）；
  - 没有**搜索**、没有**分页**、没有**排序切换**；
  - 标题只有 Playground 侧栏能看到（`first_message`）。
- `core/store.py`（SQLite）已有 `threads` 表与 `list_threads()`（`ORDER BY updated_at DESC`），具备做服务端过滤/分页的基础。

## 2. 方案

### 2.1 新页面 `/ui/threads.html`
- 独立页 + 独立资产：`assets/css/threads.css`、`assets/js/threads.js`（沿用现有 HTML/CSS/JS 分离约定与设计令牌）。
- 顶部：共享 header/nav（新增导航项「会话」），工具条：
  - **搜索框**（title / thread_id / provider / model，300ms 防抖）
  - **provider 过滤**下拉（全部 / 各 provider）
  - **每页条数**（20 / 50 / 100）
  - 刷新（+ 可选 10s 自动刷新开关）
- 列表：**卡片式**（比表格更适合标题长短不一）
  - 标题：`first_message`（为空回退 `thread_id`），单行截断 + hover 显示全文
  - 元信息：provider 徽章 · model · 状态（活跃/存档）· `updated_at`（相对时间 + 绝对时间 tooltip）
  - 操作：**继续**（跳 Playground `?thread_id=`）· **复制 ID** · **销毁**
- **加载更多**：底部按钮追加下一页；显示「已加载 X / 共 Y 条」；无更多时置灰。
- 空状态：无会话 / 无搜索结果 两种文案；加载中显示骨架。

### 2.2 状态面板瘦身
- **移除**「Thread 会话」表格。
- 保留一行摘要卡：`活跃 N / M · 共 T 条会话` + 「查看全部 →」链接到 `/ui/threads.html`。
- KPI「活跃会话」可点击 → 跳转到会话页。

### 2.3 后端：给 `/admin/threads` 加过滤/分页（向后兼容）
```
GET /admin/threads?q=<文本>&provider=<name>&limit=20&offset=0&order=desc
→ {
    "threads": [ ... ],        # 本页
    "total": 36,               # 过滤后的总数
    "limit": 20, "offset": 0,
    "has_more": true,
    "provider": "chatgpt",     # 回显
    "active": 0, "max": 8      # 兼容旧字段
  }
```
- `limit=0`（默认）= **不限制** → 旧调用方（Playground 侧栏等）行为不变。
- 过滤/排序在 **Python 侧**对「内存 + DB 合并列表」做（内存会话必须参与合并，SQL 单独分页会漏掉它们）；
  对当前量级（几十~几百条）足够，避免引入跨源分页的一致性坑。
- 新增 `ThreadManager.page(q=None, provider=None, limit=0, offset=0, order="desc")`，内部复用 `list()`；
  `list()` 保持不变。
- 匹配规则：`q` 大小写不敏感，命中 `first_message|thread_id|provider|model` 任一。

### 2.4 Playground 侧栏
- 改用服务端搜索：`refreshThreads()` 请求 `/admin/threads?q=<输入>&limit=50`
  （现在是在本地已加载列表里过滤，超出 50 条就搜不到）。
- 保留「＋ 新会话 / 刷新」。

## 3. 决策点（请拍板）

1. **分页方式**：`offset/limit` + 「加载更多」（推荐，够用且可显示总数）——还是不做分页（一次性加载 + 前端搜索）？
2. **状态面板**：彻底移除会话表格，只留一行摘要 + 入口？
3. **搜索**：服务端过滤（推荐，配合分页才正确）？Playground 侧栏也切成服务端搜索？
4. **批量清理**：需要「清空存档会话」之类的批量操作吗？（当前只能单条销毁）
5. **条数上限**：每页 20/50/100 三档可以吗？

## 4. 实施拆分（建议，一步一提交）

| # | 提交 | 内容 |
|---|---|---|
| 1 | `feat(api): /admin/threads 支持 q/provider/limit/offset/order` | `ThreadManager.page()` + 路由参数 + 单测（过滤/分页/边界/向后兼容） |
| 2 | `ui: 新增会话独立页（搜索/分页/加载更多）` | `threads.html` + css + js + nav + 静态测试 + Playwright 验证 |
| 3 | `ui: 状态面板移除会话表格，改摘要入口` | 摘要卡 + KPI 可点 + 静态测试 |
| 4 | `ui(playground): 侧栏改用服务端搜索` | `refreshThreads` 带 `q/limit` |

## 5. 风险与测试

- 风险低：改动集中在 `ThreadManager` 新增方法 + 新页面；`list()` 与旧字段保持不变。
- 测试：`tests/test_threads_pagination.py`（纯函数/管理器级，用临时 SQLite）；新页面静态断言；
  Playwright 验证「搜索→结果数」「加载更多→追加且不重复」「继续→Playground 带 thread_id」。
