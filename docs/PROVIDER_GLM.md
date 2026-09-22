# 智谱清言（chatglm.cn）接入说明

> 状态：**默认禁用（`enabled: false`）**。驱动：`src/ai_web2api/providers/glm.py`。
> 登录：**仅手动**（无密码登录：手机号验证码/扫码）。
>
> 功能本身**已校准可用**（选择器见 §2、验证见 §3），但**不适合默认启用**，原因见 §0。

## 0. 为什么默认禁用（实测结论）

| 结论 | 证据（2026-09-22） |
|---|---|
| **纯自动化过不去** | ① 宿主 headless：滑块页；② 容器 headful（Xvfb）：滑块页（`editable=0`）；③ 容器 headful + **服务同款反自动化参数 + 真实 UA**（09:21:21）：仍是滑块页 |
| **票据很短** | `acw_sc__v3` / `acw_tc` 在人工过验证后约 **27–30 分钟**过期；10:09:08 复现：容器内又回滑块页（`侧栏用户名` 命中 0） |
| **不是被登出** | 同一时刻登录 cookie **仍在**（`chatglm_token` 30 天 / `refresh_token` 180 天 / `chatglm_user_id`）→ 掉的是 **WAF 票据**，不是账号会话 |
| **真人不受影响** | 真人浏览器能执行 WAF 的 JS 挑战（环境/行为特征正常）→ 静默续期，既不掉线也看不到滑块；被拦的只有自动化环境 |
| **"绕过"不做** | 打码/滑块轨迹破解属于绕过站点反自动化控制，违背本项目"纯 DOM、不逆向、不破解风控"的原则，且会污染账号风险画像 |

**结论**：GLM 网页端只适合"**人工过一次 + 约 30 分钟内临时使用**"；长期稳定请改用**智谱开放平台官方 API**。
因此默认 `enabled: false`，避免服务里出现一个"看起来启用了、实际随时被 WAF 挡"的 provider。

### 未验证的设想：常驻页面让 JS 自动续期（**决定不追**）

理论上有一种"少打扰"的可能：WAF 的 `acw_sc__v3` 通常由**页面内 JS 挑战**按需重算，
若让**页面常驻不关**，也许人工过一次后能长期有效（不必每 30 分钟人工）。

**为什么不做**：

1. **无法在短时间内验证**：如果它只在*临近过期*才刷新，就必须**看满一个票据周期（≈30–35 分钟）**才能判定；
   只观察几分钟会得到"没刷新"的**假阴性**——结论不可信，等于白测。
2. **与本项目架构冲突**：会话按 **thread 生命周期**管理（空闲 TTL 900s、按需重建页面），
   页面本来就会被关闭/重建；要"常驻维持续期"得为 GLM 单独破例，收益小、复杂度高。
3. **即使成立也不是无人值守**：容器重启、页面被回收、票据策略变化，都还要人工介入。

**什么情况下值得回头做**：① 出现需要长期稳定使用 GLM 网页端的硬需求；
② 且愿意接受 ≥30 分钟的观测成本；③ 或先在 `enabled: true` 下实测"过期后不导航是否仍可用"。
在此之前，**长期方案请用智谱官方 API**。

### 想临时启用

```bash
# 1) config.yaml → providers.glm.enabled: true
docker compose restart                 # 配置已挂载，restart 即可（不必 rebuild）
# 2) 宿主人工过滑块并登录（打开真实可见窗口）
./scripts/login.sh glm
# 3) 之后约 30 分钟内可正常用：
curl http://127.0.0.1:8000/admin/glm/login/status      # logged_in: true
```

## 1. 站点前置：阿里云 WAF 滑动验证

| 环境 | 结果 |
|---|---|
| 宿主 headless | ❌ 「滑动验证页面：请按住滑块，拖动到最右边」 |
| 容器 headful（Xvfb） | ❌ 同样被拦（`editable=0`） |
| 容器 headful + **服务同款反自动化参数 + 真实 UA** | ❌ 仍被拦（09:21:21 UTC 实测） |

**结论：换参数/换 headless 都过不去；能过的是"人"。** 但流程可行：

```bash
./scripts/login.sh glm     # 打开真实可见窗口：先手动拖滑块过 WAF，再完成登录
```
实测（2026-09-22）：宿主完成一次人工验证后，`storage_state` 里同时带上了
**WAF cookie**（`acw_sc__v3` / `acw_tc` / `cdn_sec_tc` / `ssxmod_itna*`）与
**登录 cookie**（`chatglm_token` / `chatglm_refresh_token` / `chatglm_user_id`），
导入服务后**容器内可直接复用**（无需再过滑块，标题为「智谱清言」并显示用户名）。

> ⚠️ **保活（实测 TTL ≈ 30 分钟）**：`acw_sc__v3` / `acw_tc` 到期后，容器内**立刻退回滑块页**
> （`logged_in: false`），而登录 cookie（`chatglm_token` 30 天 / `refresh_token` 180 天）**仍然有效** ——
> 即"掉线"是 **WAF 票据过期**，不是登录过期。失效后重跑一次 `./scripts/login.sh glm` 即可。
> 因此 GLM 适合"临时用一下"，不适合长期无人值守；长期稳定建议改用**智谱开放平台官方 API**。

## 2. 实测选择器（已校准）

| 项 | 值 |
|---|---|
| 输入框 | `textarea.scroll-display-none`（**可见 textarea**，不是 contenteditable → 用 `fill()`） |
| 发送 | **无发送按钮** → `send_button: []`（回车发送） |
| **正文容器** | `.markdown-body:not(.thinking-content *)`（count=1，纯正文） |
| 思考容器 | `.thinking-content .markdown-body` |
| 为什么必须排除 | **思考与正文都用 `.markdown-body`**；直接取 `.answer-content .markdown-body` 会拿到 2 个（末条是正文，但拼接时会把思考混进来） |
| 登录判定 | `.sidebar-user-name` / `.userInfoBar`（登录后侧栏显示用户名） |
| 缓冲/拼接 | `stream_content: false` + `response_all_new: true`（表格/代码重渲染多、回答可能拆多容器） |
| 会话 URL | 形态未确认（`alltoolsdetail?…`）→ 暂不启用 thread 恢复 |

## 3. 端到端验证

```
GET /admin/glm/login/status        → logged_in: true
POST /v1/chat/completions {"model":"glm-web","thread_id":"glm-cal-1", ...}
→ content='我是智谱清言，由智谱AI开发的大语言模型GLM驱动的智能助手，可以帮你回答问题、写作创作、分析问题等。'   19.7s，无思考污染
文本保真度回归：tests/test_text_fidelity.py -k glm
```

## 4. 改版后如何重新校准

```bash
curl -X POST http://127.0.0.1:8000/admin/glm/debug/probe \
  -H 'Content-Type: application/json' -d '{"message": "只回复两个字：收到"}'
curl 'http://127.0.0.1:8000/admin/glm/debug/dom?selector=.markdown-body'   # 看正文/思考的区分是否还在
```
改完 `config.yaml` 只需 `docker compose restart`（配置已 bind mount，不必 rebuild）。
