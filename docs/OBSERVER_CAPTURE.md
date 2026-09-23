# 事件驱动抓取（Observer 模式）设计 — P2

> 状态：**设计中（待 review 后实现）**。P1（`done_toolbar`）已完成；本设计只改**抓取触发时机**，
> **不改结束判定语义**。

## 1. 目标 / 非目标

**目标**
1. 增量**更早**出现：DOM 一变化就取，不再"最多等一个轮询周期"。
2. **少做无用功**：不再每 200ms 全量 `innerHTML → Markdown`；只在"确实变了"时提取（重渲染风暴靠节流合并）。
3. `settled`（正文最后一次变化）精确到**毫秒**，让"稳定窗口"可以更短更可靠。
4. 保持**向后兼容 + 自动回退**：任何异常都退回现有 poll 路径，行为与今天一致。

**非目标**
- 不解析网络响应体（不动"不逆向内部协议"的红线）。
- 不改变结束判据的优先级（网络 close → `done_toolbar` → `stop_button` → 稳定性兜底）。
- 不追求"逐 token"级别（站点本就是分段渲染，我们跟随 DOM 粒度）。

## 2. 架构

```
        ┌─────────────────── 页面（注入，add_init_script + evaluate 双保险） ───────────────────┐
        │  MutationObserver(target = 当前回复容器, {childList, subtree, characterData})        │
        │    · 变化 → 去抖 observer_debounce_ms(80–120ms) → __aiw2a_on_mutation({seq, len})   │
        │    · childList 里发现"更新的回复容器" → disconnect + observe(new)  ← **容器自动重绑** │
        │    · 心跳：每 1s 报一次 {heartbeat:true}（用于判定"注入是否还活着"）                  │
        └───────────────────────────── expose_function ─────────────────────────────────────┘
                                  ↓ 事件（asyncio.Queue）
        Python：`_poll_response_observer()`（新）
           await queue.get(timeout=poll_interval)     ← 有事件：立刻做一次提取（含最小间隔节流）
                                                       ← 无事件：按 poll_interval 兜底提取
           提取与 diff、预览流、结束判定 → **复用现有 DOM 路径的同一套代码**
```

**关键点**：observer 只是"提取触发器"，**提取/差分/结束判定/预览流全部复用现有实现**（把 DOM 循环里的
"提取+判断"抽成一个内部函数，两条路径共用），所以不会有"两套逻辑漂移"的风险。

## 3. 配置（全部有默认值，默认仍是 poll）

```yaml
provider:
  capture_mode: poll            # poll（默认，向后兼容）| observer
  observer_debounce_ms: 100     # 页面侧去抖：合并同一帧内的多次变化
  observer_min_interval_ms: 150 # 提取最小间隔：重渲染风暴时也不至于疯狂提取
  observer_grace_seconds: 8     # 注入后这么久没有任何事件、也没有正文 → 判定注入失效，回退 poll
```

## 4. 生命周期与幂等

| 时机 | 处理 |
|---|---|
| 发送前 | 注入（`page.evaluate` 安装；**幂等**：先 `disconnect()` 旧的、清计数） |
| 页面 reload / SPA 导航 | 事件停止 → `observer_grace_seconds` 内无事件且无正文 → **重新注入一次**，再失败 → 回退 poll |
| thread 复用（resume） | 每次请求前重新注入（容器可能已变） |
| 请求结束 | **不**断开观察（页面留给 ThreadManager）；但把 window 上的回调标记为"本轮无效"（`seq` 世代号），避免串轮 |
| 回退 | 记 `timeline.note("capture", "observer→poll")` + `logger.warning`，其余逻辑不变 |

**世代号（generation）**：每次请求注入时 `window.__aiw2a_gen = ++n`，事件里带 `gen`；Python 只接受当前世代的事件 → 避免上一轮残留事件污染本轮。

## 5. 度量（timeline 扩展）

| 字段 | 含义 |
|---|---|
| `capture` | `poll` / `observer` / `observer→poll`（回退） |
| `events` | 收到的变化事件数（去抖后） |
| `extract_calls` | 实际提取次数（**用于证明"少做无用功"**） |
| `emit_lag_ms_avg/max` | 事件发生 → 我们提取到并产出增量的延迟（页面侧时间戳 vs Python 时间，做单调性校正） |
| `observer_fallback` | 1 = 发生过回退 |

## 6. 测试计划

**单元（无需浏览器）**
1. 节流合并：100ms 内 20 个事件 → 只有 1 次提取，且内容完整；
2. 最小间隔：连续事件间隔 < `observer_min_interval_ms` → 提取被推迟但不丢内容（最终 diff 补齐）；
3. 超时回退：`observer_grace_seconds` 内无事件且无正文 → 切 poll 并记 note；
4. 世代号：旧世代事件被丢弃；
5. 幂等注入：连调两次 → 只保留一个 observer（用假 window 断言 `disconnect` 被调）。

**集成（fake page，无网络）**
6. 事件驱动下：正文分 5 段推入 → 产出增量条数 ≥ 3 且拼接 == 最终文本（不丢不重）；
7. 与预览流/缓冲组合（`stream_content: false` + `preview_stream: true`）行为一致。

**Live（真站点，题目从文本库随机取）**
8. ChatGPT / DeepSeek 各 3 例：保真度覆盖率 ≥ 0.95、无思考泄漏；
9. timeline 对比：`extract_calls` 与 `extract_ms_total` **明显下降**、`emit_lag_ms_max` ≤ 去抖+间隔；
10. 回退演练：故意在注入前让页面导航 → `observer_fallback=1` 且结果仍正确。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 站点重渲染风暴 → 提取更频繁（比轮询更糟） | 页面侧去抖 + Python 侧最小间隔 + `extract_calls` 度量把关 |
| 注入被 CSP/时机阻断 | 与现有网络钩子同一机制（已验证可用）；grace 内无事件 → 回退 |
| 容器被替换（多轮/工具卡/重新生成） | childList 时**自动重绑**到最新容器（参考脚本的核心手法之一） |
| 与 FC / 附件 / 线程恢复相互作用 | 两条路径共用同一提取+判定函数；回归快速套件 + 保真度 |
| 回调跨轮污染 | 世代号过滤 + 每轮重注入 |

## 7.5 Spike 实测（2026-09-22，在服务真实页面上用 CDP 验证）

| 验证项 | 结果 |
|---|---|
| **容器未出现时注入** | ❌ 直接 `observe(最新回复容器)` 会得到 `NO_CONTAINER`（发送后容器才挂载）→ **必须 `observe(document.body)` 并"容器出现时自动重绑"**（这一点从"优化项"升级为**实现必需**） |
| `observe(document.body)` 注入 | ✅ 成功（返回 `INSTALLED_ON_BODY`） |
| 事件延迟 / 提取次数对比 | ⏸ **未取到**：ChatGPT 当时被站点降级（页面卡加载屏、provider 报未登录）→ 无生成可测。改为**实现后用 timeline 度量**（`extract_calls` / `emit_lag_ms_*`），这正是 §5 的用途 |
| CDP 连接现有页面做诊断 | ✅ 稳定可用（`browser.debug_port`），且**不会**因新开 context 触发风控 |

> 结论：设计不变，但 §4 的注入序列里把"**先观察 body → 发现新容器即重绑**"列为必需步骤；
> 首字延迟/提取次数的量化，放到实现后由 timeline 给出（避免为了 spike 继续加压站点）。

## 8. 落地顺序（每步可独立回退）

1. **抽公共函数**（纯重构，无行为变化）：DOM 循环里的"提取 → diff → 预览流 → 结束判定"抽成内部方法，poll 路径改为调用它；跑全套 + 保真度。
2. **实现 observer 注入 + 事件队列 + 回退**（`capture_mode: observer`，默认仍是 poll）。
3. **度量接入 timeline**（`capture/events/extract_calls/emit_lag`）。
4. **ChatGPT 试点**（配置打开 observer）→ 保真度 3/3 + timeline 对比；达标后再考虑默认打开。
