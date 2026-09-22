# 配置热重载 + UI 动态启用/停用（**暂缓**，设计存档）

> 状态：**暂缓（deferred）**。当前做法：改 `config.yaml` 后 `docker compose restart`（配置已 bind mount，
> **不需要 rebuild**）。触发条件见文末。

## 1. 要解决的问题

`providers.<p>.enabled` 以及选择器/超时等配置**只在进程启动时读取** → 改完必须 `restart`（约 20s）。
而"调选择器"是本项目最频繁的动作（Kimi/豆包/GLM 的接入过程中都在反复调），
另外 UI 上也看不到 `enabled` 的实时状态 → 容易出现"改了没生效"的困惑。

**目标**：在 UI 上直接启用/停用 provider、调整配置后**秒级生效并持久化**；不用 restart。

## 2. 硬约束（决定了实现方式）

`create_router(registry, threads)` 与各路由都是**闭包持有 `registry` 对象**的 →
热重载**不能替换对象**（否则路由仍指向旧实例），必须**就地重建** registry 内部结构
（`_providers` / `_model_map` / `_aliases` / `_alias_owner`）。

## 3. 方案（3 个提交，约半天）

### C1 `ProviderRegistry.reload(config)`（就地重建）+ `POST /admin/reload`
- 重建前检查：任一 provider 的 `gate.busy` → **拒绝**（503「有请求正在执行，稍后重试」），
  避免"旧实例 + 新实例"同时操作同一个页面/context；
- `threads.close_all()` 销毁活跃会话（客户端下次同 id 请求自动重建）；文档写清这一点；
- 被**移除/停用**的 provider：`browser.reset_context(name)` 释放页面与 context；
- 重建 `default_provider` 兜底别名（`OPENAI_COMMON_MODELS` 的挂载逻辑复用同一段代码）；
- `_login_status` 保留"有 state.json 即视为已登录"的种子逻辑；
- 接口返回**差异摘要**：新增 / 移除 / enabled 变化 / 模型变化。

### C2 持久化开关 + UI
- `POST /admin/{p}/enabled` body `{"enabled": true|false}`：
  **外科式改 `config.yaml`**（只改该 provider 块的 `enabled:` 一行，保留注释与格式）→ 自动触发热重载；
- UI：provider 详情加「启用 / 停用」开关（停用前若有活跃 thread → 二次确认），顶栏加「重载配置」。

### C3 文档/README
- README/`docs/MANUAL_LOGIN.md` 写清：**热重载只覆盖 `config.yaml`**；
  **代码（`src/**`）仍需 rebuild**；`.env` 仍是容器启动时注入（`load_dotenv` 在启动读）。

## 4. 边界（必须写清，避免误解）

| 改了什么 | 热重载后 | 仍需 |
|---|---|---|
| `config.yaml`（开关 / 选择器 / 超时 / provider 增删） | ✅ 秒级生效 | — |
| `src/**` 代码 | ❌ | `docker compose up -d --build` |
| `.env` | ❌ | `docker compose restart` |
| `docker-compose.yml`（挂载/端口/env） | ❌ | `docker compose up -d`（重建容器） |

## 5. 风险

- 集中在"运行时重建注册表"：in-flight 请求（用 `gate.busy` 拦）、活跃会话（`close_all`）、
  兜底别名与 `_model_map` 的重建一致性 —— 均可单测覆盖；
- 外科式改 YAML 只动一行（正则匹配该 provider 块内的 `enabled:`），不动其他内容与注释；
- 与 `docs/PER_PROVIDER_HEADLESS.md` 的"浏览器分组"无耦合，可独立实施。

## 6. 触发条件（何时值得做）

- 需要频繁切换 provider 开关（例如临时启用 GLM 用 30 分钟再关）；
- 选择器调试频率升高，20s restart 成为明显摩擦；
- 或希望把 `/ui` 做成"不看配置文件也能运维"的形态。
