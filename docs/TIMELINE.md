# 请求时间线（通用度量）：一次请求"慢在哪"

> 状态：**已实现**（日志 + 内存缓冲；后续可入库）。所有 provider、所有通道共用。

## 1. 为什么要它
"感觉慢"很难查：是页面在写？我们没出字？站点在排队？—— 先有**客观时间点**，才能定向优化。

## 2. 关键时间点

```
setup ── send ── first_think ── first_content ── settled ── done ── final
页面就绪  已送达     思考首字       正文首字       正文不再变  我们判定结束  收尾
```
| 时间点 | 含义 |
|---|---|
| `setup` | 输入框就绪、模型/开关/附件就绪（即将发送） |
| `send` | 提示词**已送达站点**（点击发送且确认输入框清空） |
| `first_think` | 首个**思考**增量 |
| `first_content` | 首个**正文**增量 |
| `settled` | 正文**最后一次变化**（≈站点写完；每次变化覆盖打点） |
| `done` | **我们**判定"生成结束" |
| `final` | 生成器收尾（定稿补发、组件捕获、session 统计） |

派生指标：
| 指标 | 计算 | 看什么 |
|---|---|---|
| `ttft` | `first_content - send` | **首字延迟**（用户感知；大＝站点侧慢/排队） |
| `settle_lag` | `done - settled` | **"白等"**：我们比站点慢多少（大＝结束判定太保守） |
| `tail` | `final - done` | 收尾开销（大＝定稿补发/组件捕获重） |
| `total` | `final` | 本次请求总耗时（相对 generate 入口） |

计数：`polls`（轮询次数）、`extract_ms_total`（抓取总耗时）、`deltas`、`chars`、`think_chars`，
以及 `path`（`net`/`dom` 数据通道）、`finalize`（`net_close`/`stop_button`/`stability`/`timeout_fallback`）。

## 3. 日志格式（单行 key=value，可直接 grep/入库）

```
[timeline] provider=doubao model=doubao-web thread=create path=dom setup=0.04 send=1.44 \
  first_think=- first_content=16.77 settled=3.64 done=16.35 final=16.77 \
  ttft=15.33 settle_lag=12.72 tail=0.42 chars=2 deltas=1 \
  extract_ms_total=479.11 polls=61 finalize=stability
```
```bash
docker compose logs | grep '\[timeline\]'                  # 全部
docker compose logs | grep '\[timeline\]' | grep settle_lag | tail -20
```

## 4. 实测对照（2026-09-22，同一批短问答）

| provider | path | ttft | **settle_lag** | tail | finalize |
|---|---|---|---|---|---|
| deepseek | **net** | 0.62s | **0.00s** | 0.00s | `net_close` |
| chatgpt | dom | 4.46s | **1.28s** | 0.41s | `stop_button` |
| doubao | dom | 15.33s | **12.72s** | 0.42s | `stability` |
| chatgpt（表格题） | dom | 2.54s | 13.55s | 0.41s | `stop_button`（**站点侧**，非我们的问题） |

**读法（诊断处方）**
- `ttft` 大 → 站点侧（排队/限流/自身思考）。例：豆包首字 15s。
- `settle_lag` 大 → **要结合 `finalize` 判断责任方**（这是最容易误判的一项）：
  | `finalize` | 含义 | 该怎么做 |
  |---|---|---|
  | `stability` / `timeout_fallback` | **我们的判据保守**（站点信号没用上） | 调 `stable_polls`/`min_wait_before_stable`，或补一个更快的结束信号（停止按钮 / 消息工具栏 / 网络 `close`）。例：豆包 `settle_lag=12.72s`，正文 3.64s 就不再变 → **当前最大可优化项** |
  | `stop_button` / `net_close` | **站点侧仍在生成**（我们跟着站点） | 一般不用动；若要更快，只能"提前收尾"（有截断风险）。例：ChatGPT 表格用例 `settle_lag=13.55s`，正文 5.55s 完成但站点到 19.1s 才结束 |
  | `timeout_fallback` | 站点卡住/限流，我们兜底返回已生成内容 | 看 `net`/错误信息定位站点侧 |

- `tail` 大 → 定稿补发/组件捕获/收尾逻辑重。
- `extract_ms_total/polls` 大 → 抓取本身重（选择器太宽/页面太大）。
- `path=net` 相比 `dom`：结束信号是硬的（`net_close`）→ `settle_lag` 天然为 0。

## 5. 怎么查历史（内存缓冲）

```
GET /admin/timeline?limit=50              # 最近 50 条（新→旧）
GET /admin/timeline?provider=doubao&limit=20
```
返回项含 `marks` / `counts` / `ttft` / `settle_lag` / `tail` / `total`，形状即"未来入库的行"。

## 6. 后续入库（预留）
`RequestTimeline.as_dict()` 已是**纯数据**（provider/model/thread/path/finalize/marks/counts/ttft…）。
要长期留存时：把 `core/timeline.py` 里的 `TimelineLog.record()` 换成写 SQLite/时序库即可，
调用方（provider 层）无需改动。

## 7. 打点的位置（实现要点）
- **provider 层**（`providers/webchat.py`）：唯一打点处 → 所有 provider、流式/非流式、`net`/`dom` 全覆盖。
- `settled` 打在**正文真正变化**的时刻（不是"chunk 发出去"的时刻）——缓冲模式下 chunk 是定稿后才发的，
  按 chunk 打会得到 `settled > done`（负 `settle_lag`），指标就没意义了。
- 无时间线时（单元测试直接调轮询）打点是 **no-op**。
