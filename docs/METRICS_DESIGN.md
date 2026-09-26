# Provider Metrics 设计

> 目标：每次请求记录耗时 + 成功与否 → UI 按 provider 展示（成功率、平均耗时、P50/P95） → 选 provider 时参考。

## 1. 采集什么（每请求一条）

| 字段 | 来源 | 说明 |
|---|---|---|
| `provider` | timeline | provider 名 |
| `model` | timeline | 模型 |
| `thread_id` | timeline | 会话（无状态请求 = `stateless`） |
| `ok` | 生成结果 | 1=成功（非空 content/reasoning），0=异常/空 |
| `total` | timeline | `final − started`（总耗时 s） |
| `ttft` | timeline | `first_content − send`（首字延迟 s） |
| `settle_lag` | timeline | `done − settled`（"白等" s） |
| `tail` | timeline | `final − done`（收尾 s） |
| `finalize` | timeline | `stability / stop_button / net_close / timeout_fallback / exception` |
| `error` | 异常 | 异常消息（仅 ok=0） |
| `created_at` | DB | 时间戳 |

不在库里记：prompt / content / cookies / 个人身份信息。

## 2. 存哪里

复用 `profiles/threads.db`（已 Docker 卷挂载），加一张 `metrics`：

```sql
CREATE TABLE IF NOT EXISTS metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    provider   TEXT NOT NULL,
    model      TEXT,
    thread_id  TEXT DEFAULT 'stateless',
    ok         INTEGER NOT NULL DEFAULT 1,
    total      REAL,
    ttft       REAL,
    settle_lag REAL,
    tail       REAL,
    finalize   TEXT,
    error      TEXT,
    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS ix_metrics_provider ON metrics(provider, created_at);
```

写路径：provider `generate()` 返回后（`finally` 块），调 `store.record_metric(...)`（同步，用 `asyncio.to_thread` 包）。异常路径也记（ok=0, error=exc, total=已耗时间）。

## 3. 聚合 API

`GET /admin/metrics?provider=&window=24h`

`window`: `1h` / `24h` / `7d` / `all`（默认 24h）

返回每个 provider 一行：

```json
{
  "window": "24h",
  "providers": [
    {
      "name": "doubao",
      "n": 42,
      "ok_n": 40,
      "success_rate": 95.2,
      "avg_total": 14.7, "p50_total": 12.1, "p95_total": 28.3,
      "avg_ttft": 3.2,
      "finalize_counts": {"stability": 38, "timeout_fallback": 4},
      "last_ok": "2026-09-26T12:34:00",
      "last_error": null
    }
  ],
  "ranking": ["doubao", "gemini", "yuanbao", ...]
}
```

p50/p95 在 Python 端算（SQLite 无百分位函数，数据量小，够用）。

## 4. UI（状态面板扩展）

在 `/ui/` 状态面板的 **Provider** tab 下，每个 provider card 加一行指标：

```
┌─ doubao ───────────────────────────────────────┐
│  ✅ 95%  ·  avg 14.7s  ·  ttft 3.2s  ·  42次    │
│  finalize: stability×38 timeout×4               │
│  ████████████████░░  rank #1                    │
└─────────────────────────────────────────────────┘
```

- 成功率 < 80% 红标，80-95% 黄，≥95% 绿
- 点击 card 展开最近 10 条时间线（复用 `/admin/timeline`）
- 顶部加 "Metrics" KPI：总请求数、整体成功率、当前最快 provider

新增页面可选：`/ui/metrics.html`（仅指标，带图表 Canvas，折线=每分钟请求数，柱=各 provider 平均耗时对比）。MVP 先做 status 面板嵌入，图表后加。

## 5. 选 provider 的参考算法（可选，开关控制）

配置文件加：

```yaml
metrics:
  enabled: true               # 默认 true；关闭则只记不用
  window_hours: 24            # 滚动窗口
  min_samples: 5              # 少于 N 次不参与排名
  alpha: 0.6                  # 成功率权重
  beta:  0.3                  # 速度权重（1/avg_total 归一化）
  gamma: 0.1                  # 新鲜度权重（最近 1h 成功率）
```

评分（每 provider）：

```
score = alpha * success_rate
      + beta  * (1 / avg_total)  normalized across candidates
      + gamma * recent_success_rate_1h
```

使用点：
- `cli.py` auto-detect / multi-provider fallback：分数高的先试
- `/v1/chat/completions` 如果配了 `model: "auto"` → 按评分选 provider
- 人工可见：UI ranking 列

默认 `enabled: false` —— 先开环记录，验证数据准确后再启用自动选择。

## 6. 实现步骤（建议）

1. `core/store.py`：加 `metrics` 表 + `record_metric()` + `aggregate_metrics(window)` CRUD
2. `core/timeline.py`：`RequestTimeline` 加 `ok`/`error` 字段；`log()` 时同时 `TIMELINES.record()` + 写库（通过 `store`，异步）
3. `cli.py` / provider `generate()`：`try/except/finally` 里填 ok/error，触发 `record_metric`
4. `api/routes.py`：`GET /admin/metrics`
5. `webui/`：状态面板 provider card 嵌入指标；新增 `/ui/metrics.html`（可选）
6. tests：`test_metrics_*`（记录/聚合/窗口/空库）

## 7. 待决定

- 保留多久？24h / 7d / 30d 自动清理？→ 建议 7d，超过删（`DELETE FROM metrics WHERE created_at < ...`）
- 成功判定：content 非空？还是 reasoning 也算？→ 两者任一非空 = ok
- 是否记录每条消息的内容摘要？→ 不记，只记长度（`chars`）可选
- 是否区分"网络超时"和"站点返回错误"？→ `finalize` 字段已覆盖大部分；error 文本补细节
