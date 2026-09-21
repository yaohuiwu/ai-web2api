# 会话历史持久化（SQLite）与 Playground 改造 · 修改计划

> 状态：**进行中**（按下方「分步计划」逐步实现，每步独立提交）
> 关联文档：[`DESIGN.md`](DESIGN.md)

## 1. 背景与目标

现状问题：

1. **历史消息不落盘**。`profiles/<provider>/threads.json` 只存 `{url, model, title}`，
   对话内容只在浏览器内存里，切换到某会话/重启服务后聊天区被清空。
2. **Playground 输入框在当前输入法（IME）组词时按回车会误发送**。

目标：

- 用 **SQLite** 持久化「会话元数据 + 消息内容（user/assistant + 思考）」；
- `threads.json` 迁移进库后**删除**；
- Playground 切换会话时展示历史消息；左侧会话列表改由 DB 提供，并加「刷新」按钮、
  **去掉定时轮询**；
- 修复 IME 组词回车误发送。

### 已确认的决策

| # | 决策 |
|---|------|
| 3 | `DELETE /admin/threads/{id}` **连历史消息一起删**（会话废弃语义） |
| 5 | `threads.json` 迁移后**直接删除**（不留 `.bak`） |
| 6 | 左侧会话列表**切换为 DB 数据源**；新增刷新按钮；**移除 `setInterval` 轮询** |

### 采用的默认项（如无异议按此实现）

| # | 默认 |
|---|------|
| 1 | DB 路径 `profiles_dir / "threads.db"`（Docker 卷 `web2api-profiles` 已覆盖，开箱持久化） |
| 2 | 持久化总开关沿用 `server.thread_persist`（默认 `true`）；为 `false` 时不写库（与现有行为一致） |
| 4 | assistant 的 `reasoning`（思考过程）**一起入库**，Playground 以可折叠块展示 |

---

## 2. 数据模型

DB 文件：`profiles/threads.db`，启动时启用 `PRAGMA journal_mode=WAL`、
`PRAGMA foreign_keys=ON`。

```sql
CREATE TABLE IF NOT EXISTS threads (
  thread_id  TEXT PRIMARY KEY,
  provider   TEXT NOT NULL,
  model      TEXT,
  title      TEXT,
  url_id     TEXT,                -- provider 会话 id（DeepSeek /a/chat/s/<uuid>），原 threads.json 的 url
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id  TEXT NOT NULL REFERENCES threads(thread_id) ON DELETE CASCADE,
  role       TEXT NOT NULL,       -- user | assistant
  content    TEXT NOT NULL DEFAULT '',
  reasoning  TEXT,                -- assistant 的思考过程（user 为 NULL）
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, id);
```

- 一个 `thread_id` 一条 `threads` 行；消息按自增 `id` 排序（同一 thread 的请求由
  `session.lock` 串行，不会乱序）。
- 连接：`check_same_thread=False` + `threading.Lock` 串行化写；对外用
  `asyncio.to_thread` 包装，避免阻塞事件循环。

---

## 3. 模块改动

### 3.1 新增 `src/ai_web2api/core/store.py`

`ThreadStore`（同步方法，便于单测）：

| 方法 | 说明 |
|------|------|
| `upsert_thread(thread_id, provider, model, title, url_id=None)` | 建/更新会话行 |
| `get_thread(thread_id) -> dict \| None` | 单条 |
| `list_threads(provider=None) -> list[dict]` | 列表（`updated_at` 倒序） |
| `set_url_id(thread_id, url_id, model, title)` | 落盘会话 id（原 `persist()`） |
| `delete_thread(thread_id)` | 删会话 + 级联删消息 |
| `append_messages(thread_id, [ (role, content, reasoning) ])` | 追加消息 |
| `get_messages(thread_id) -> list[dict]` | 按 `id` 升序 |
| `migrate_from_json(profiles_dir) -> int` | 迁移并删除 `threads.json`，返回导入条数 |

### 3.2 改造 `src/ai_web2api/core/threads.py`

- 删除 JSON 持久化（`_persist_path/_load_urls/_save_urls`），改走 `ThreadStore`。
- `get_or_create()`：create 时 `upsert_thread(..., title=first_message)`；
  restore 时从 store 读取 `url_id/model/title`。
- `persist()` → `set_url_id(...)`（保留「无变化不写」短路）。
- `list()` → 内存活跃会话 + `store.list_threads()` 合并（保留 `loaded` 标记）。
- `close(discard=True)` → `delete_thread()`（级联删消息）。
- 新增 `save_turn(thread_id, user_text, assistant_text, reasoning)` 与
  `get_messages(thread_id)`。
- 启动时调用 `store.migrate_from_json()`（迁移旧 `threads.json` 后删除文件）。

### 3.3 `src/ai_web2api/api/routes.py`

- 请求**正常完成后**入库：
  - 非流式：`provider.complete` 返回后 `save_turn(...)`；
  - 流式：`_gen()` 内累积 `content/reasoning`，循环结束后 `save_turn(...)`；
  - `user_text` 取 `last_user_message(messages)`（与实际发给页面的一致）。
- 新增 `GET /admin/threads/{thread_id}/messages`
  → `{"thread_id", "messages": [{role, content, reasoning, created_at}, ...]}`。
- `DELETE /admin/threads/{id}` 语义：关页面 + 删历史。

### 3.4 `src/ai_web2api/webui/playground.html`

- `switchThread(tid)`：请求 `/admin/threads/{tid}/messages`，清空聊天区后逐条渲染
  （user 气泡 / assistant 气泡 + 可折叠思考块）。
- 左侧列表：数据源改 DB；新增**刷新按钮**；**移除 `setInterval(refreshThreads, 5000)`**；
  发送完成 / 新建 / 切换后手动刷新一次。
- 修复 IME 误发送（见第 4 节）。

### 3.5 配置

- 沿用 `server.thread_persist` 作为「会话 + 消息」持久化总开关。
- DB 路径派生自 `profiles_dir`，暂不新增配置项（可后续按需加 `server.history_db`）。

---

## 4. IME（输入法）回车误发送修复

`#input` 的 `keydown` 增加组词保护：

```js
let composing = false;
input.addEventListener("compositionstart", () => { composing = true; });
input.addEventListener("compositionend",  () => { composing = false; });
input.addEventListener("keydown", (e) => {
  if (e.isComposing || e.keyCode === 229 || composing) return; // 组词中，回车=选词
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
```

再加「`compositionend` 后 ~80ms 内忽略 Enter」的兜底，覆盖部分 IME 确认候选词时
`isComposing` 已为 `false` 的边界。

---

## 5. 迁移策略

- 启动时对每个 provider 检查 `profiles/<provider>/threads.json`：
  `upsert` 进 `threads` 表（不覆盖已存在行），**成功后删除该文件**。幂等。
- 旧 JSON 无消息内容，历史消息从迁移之后开始记录；会话可恢复能力（`url_id`）不丢。

---

## 6. 分步计划（每步检查通过后单独提交）

- [x] **Step 0**：本计划写入 `docs/HISTORY.md`。（提交）
- [x] **Step 1**：新增 `core/store.py` + `tests/test_thread_store.py`（CRUD / 排序 /
      级联删除 / JSON 迁移）。独立可测，不接入业务。（提交）
- [x] **Step 2**：重构 `core/threads.py` 走 `ThreadStore`，启动迁移并删除 `threads.json`；
      补/改测试。（提交）
- [ ] **Step 3**：`api/routes.py` 完成消息落库 + `GET /admin/threads/{id}/messages`；
      扩展 `tests/test_openai_compat.py`（有登录时才跑）。（提交）
- [ ] **Step 4**：`playground.html` 修复 IME 误发送 + 静态断言测试。（提交）
- [ ] **Step 5**：`playground.html` 历史消息渲染 + 刷新按钮 + 去掉轮询。（提交）
- [ ] **Step 6**：回归验证（Docker 起服务，多轮对话 / 切换 / 重启 / 中文输入法），
      更新 `docs/DESIGN.md` 与 `README.md`。（提交）

---

## 7. 验收清单

1. 发多轮消息后，`GET /admin/threads/{id}/messages` 返回完整 user/assistant（含思考）。
2. Playground 切换到某会话，聊天区展示该会话历史。
3. 重启服务（含容器重建）后历史仍在；旧 `threads.json` 被迁移并删除。
4. 左侧列表有刷新按钮，不再每 5s 轮询 `/admin/threads`。
5. 中文输入法组词时按回车只上屏候选词，不发送。

---

## 8. 非目标 / 暂不做

- 附件内容入库（本次只存文本 + 思考）。
- 多进程 / 多 worker 共享 DB 的并发方案。
- 跨 provider 会话合并、历史编辑/删除单条消息。
