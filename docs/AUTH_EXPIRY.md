# 认证有效期展示与「快过期」提醒（设计）

> 状态：**P1 已实现**（P2 待定）。目标：UI 展示每个 provider 的认证有效期；对**手动认证**（`login.mode=manual`，如 chatgpt）在快过期时提醒用户更新认证信息。
>
> P1 落地：`login.auth_cookies` / `expiry_warn_days` / `browser.auth_expiry_warn_days` 配置 与 `config.yaml` 默认；`core/auth_expiry.py` 计算；`/admin/status` 的 `auth_expiry` 字段；UI 「认证有效期」行 + 手动认证提醒卡；后台仅对手动认证的 WARNING。

## 1. 目标 / 非目标

**目标**
- `/ui` provider 详情展示「认证有效期」（绝对时间 + 剩余天数 + 状态色）。
- **仅对手动认证**（`login.mode=manual`，如 chatgpt）在**快过期 / 已过期**时给出醒目提醒（复用现有「未登录引导卡 + 导入面板」组件）。
- **自动认证**（`login.mode=auto/cookies`，如 deepseek）**只展示过期信息，绝不提醒** —— 无需人工干预。
- 保持通用框架通用：**配置驱动、可选**，未配置不改变现有行为。

**非目标**
- 不逆向/预测服务端真实会话有效期（只依据本地 cookie 的 `expires` 或配置的 TTL 估算）。
- 不自动重登手动 provider（manual 的语义就是"手动"）。
- 「过期」≠「掉线」：cookie 可能被服务端续期，提醒是**提示**而非状态判定。

## 2. 现状与实测数据（决定方案的关键）

当前 `BrowserManager.state_expiry()` 取「**最早到期**的 cookie」——会命中与登录无关的 WAF/偏好 cookie：

| provider | cookie | 到期 | 是否认证相关 |
|---|---|---|---|
| chatgpt | `__Secure-next-auth.session-token.0/.1` | 89.5 天 | ✅ |
| chatgpt | `oai-client-auth-info` | 29.5 天 | ⚠️ 可能相关 |
| chatgpt | `SID` / `__Secure-1PSID` / `HSID` … | 399 天 | ❌ Google 账号 cookie |
| chatgpt | `__Secure-next-auth.csrf-token` | 会话(-1) | ❌ |
| deepseek | `aws-waf-token` | **3.1 天** | ❌ WAF |
| deepseek | `ds_session_id` | 会话(-1) | ❓ 真认证但无本地过期时间 |
| qwen | `token` / `refresh_token` | 29.2 / 29.3 天 | ✅ |

**结论**：必须由配置显式声明"哪些 cookie 代表认证"，不能靠「最早到期」。

### 2.1 为什么 DeepSeek **拿不到**过期时间（实测）

| 位置 | 内容 | 能否读出过期 |
|---|---|---|
| cookie `ds_session_id` | 会话型 `expires=-1` | ❌ |
| cookie `aws-waf-token` | 3.1 天后 | ❌ WAF，非登录态 |
| localStorage `settingsJwt` | JWT，但 `{"alg":"dir","enc":"A256GCM"}` → **JWE，5 段，payload 加密** | ❌ 解不开 |
| localStorage `userToken` | 64 字节二进制（非 JWT） | ❌ 无 `exp` |

→ 结论：DeepSeek **无法提前知道**过期时间（只能**事后**由掉线/401 检测到「已失效」）。
因其为 `login.mode=auto`（只展示不提醒），UI 直接显示**「未知」**即可。
若日后想要预估，可选：`session_ttl_days`（上次成功登录时间 + TTL，标注「估算」），或长期记录实际掉线时长做经验值（P2）。

## 3. 配置（配置驱动，全部可选）

`config.yaml`：

```yaml
browser:
  auth_expiry_warn_days: 3.0        # 全局默认提醒阈值（天）

providers:
  chatgpt:
    login:
      mode: manual
      auth_cookies:                 # 代表登录态的 cookie（glob，大小写不敏感）
        - "__Secure-next-auth.session-token*"
      expiry_warn_days: 7           # 可选，覆盖全局（手动认证建议更早提醒）
  qwen:
    login:
      mode: auto
      auth_cookies: ["token", "refresh_token"]
  deepseek:
    login:
      mode: auto
      # auth_cookies: []     # 显式空/不写 = 无从判断（UI 显示「未知」）
      session_ttl_days: null        # 会话型 cookie 无 expires 时的估算 TTL（可选）
```

新增字段（`LoginConfig`）：`auth_cookies: list[str] = []`、`session_ttl_days: float | None = None`、`expiry_warn_days: float | None = None`；新增 `BrowserConfig.auth_expiry_warn_days: float = 3.0`。

> 默认 `config.yaml` 里**直接给 chatgpt / qwen 配好**，开箱即可看到真实天数；deepseek 显示「未知」。

## 4. 计算规则（新增 `core/auth_expiry.py` 或 `BrowserManager.auth_expiry()`）

```
cookies = state.json 的 cookies（按 mtime 缓存，避免 10s 轮询反复读盘）
matches = [c for c in cookies  if 任一 fnmatch(c.name, pat) for pat in auth_cookies]

if matches 中有 expires>0:
        expires_at = min(那些 expires)；source = "cookie"
elif matches 全是会话(-1) 且 session_ttl_days:
        expires_at = state.json mtime + session_ttl_days；source = "session_estimate"
else:
        expires_at = None；source = "unknown"
```

- **未配置 `auth_cookies` → `unknown`**（**不**回退到「最早 cookie」，避免 deepseek 那种 `aws-waf-token` 误报）。
- 读取顺序：优先实时 context（比 state.json 新）；否则 state.json。

**状态机**

| state | 条件 |
|---|---|
| `logged_out` | `logged_in == False` |
| `unknown` | `expires_at is None` |
| `expired` | `now >= expires_at` |
| `soon` | `days_left <= warn_days`（`warn_days` = provider 覆盖 or 全局默认） |
| `ok` | 其余 |

## 5. 接口输出（`GET /admin/status` → `providers[i].auth_expiry`）

```json
"auth_expiry": {
  "state": "soon",                        // ok | soon | expired | unknown | logged_out
  "expires_at": 1797774087.0,             // epoch 秒；unknown 为 null
  "expires_at_iso": "2026-12-20T12:00:00Z",
  "days_left": 6.4,                       // 可为负（已过期）
  "warn_days": 7,
  "source": "cookie",                     // cookie | session_estimate | unknown
  "cookie": "__Secure-next-auth.session-token.0"
}
```

`/healthz` 保持轻量：仅加 `"auth_state": "soon"`（可选，P2）。

## 6. UI

> 提醒规则：**仅 `login.mode == "manual"`** 且 `state in {soon, expired}` 才提醒；`auto`/`cookies` 只展示不提醒。

1. **详情页新增一行「认证有效期」**（所有 provider 都显示）
   - `ok`：`2026-12-20（还有 89 天）`
   - `soon`：黄色 `⚠ 6 天后过期（2026-09-24）`
   - `expired`：红色 `已过期（2026-09-24）`
   - `unknown`：灰色 `无法判断（会话型/未配置关键 cookie）`
2. **手动认证提醒卡**（`login_mode == "manual"` 且 `state in {soon, expired}`）——**复用现有 `.login-cta` 组件**：
   > ⚠ **chatgpt 认证约 6 天后过期**。该 provider 为手动认证，请在过期前更新：
   > `python -m ai_web2api.cli login chatgpt`
   > [复制命令] [导入登录态]（点了展开现有导入面板）
3. **provider 标签圆点**（P2）：`ok` 绿 / `soon` 黄 / `expired`·`logged_out` 红。
4. **顶栏横幅**（P2）：存在任一 `soon/expired` → 「N 个 provider 认证即将过期」。

## 7. 后台行为

- `status_check` 循环里检测到 `soon/expired` → `logger.warning` 一次（**状态变化时才记**，避免刷屏）。
- 不弹窗、不桌面通知、不自动重登。

## 8. 测试

- 单测（mock cookie 列表）：glob 匹配 / 无匹配→unknown / 会话型+TTL 估算 / 多 cookie 取最早 / `ok|soon|expired|unknown` 边界（含 `days_left == warn_days`）/ provider 覆盖阈值。
- `/admin/status`：字段存在性 + 形状。
- UI：静态断言（`auth-expiry` 渲染分支存在）+ 拦截 `/admin/status` 注入 `soon` 的 Playwright 渲染验证（复用现有手法）。
- 用真实 `profiles/*/state.json` 做 fixture 采样（已验证可用）。

## 9. 分阶段

- **P1（已实现）**：配置字段 + 计算模块 + `/admin/status` 字段 + 详情「认证有效期」行 + 手动认证提醒卡 + WARNING 日志 + 测试。实测：chatgpt `ok` 90 天 / qwen `ok` 29 天 / deepseek `unknown`。
- **P2（可选）**：tab 圆点 / 顶栏横幅 / `/healthz` 标记 / webhook / 统一 `state_expiry()`（落盘判定）到同一套 cookie 选择逻辑 / 实时 context 优先。

## 10. 待确认（review 点）

1. ~~`auth_cookies` glob 显式声明 or 自动推断~~ → **已定：显式配置**（自动推断会被 `SID`/`aws-waf-token` 带偏）。
2. ~~未配置时显示「未知」还是「估算」~~ → **已定：未知**（不误报）。
3. **阈值**默认 3 天、手动认证 7 天 —— 待确认。
4. ~~提醒形式~~ → **已定：仅手动认证提醒**；顶栏横幅/圆点放 P2。
5. ~~`session_ttl_days`~~ → **已定：DeepSeek 拿不到明确过期时间（见 §2.1），auto 不提醒 → 显示「未知」**；`session_ttl_days` 作为可选预留字段。

---
*参考：`docs/MANUAL_LOGIN.md`（手动登录与导入）、`src/ai_web2api/browser/manager.py:state_expiry`、`src/ai_web2api/api/routes.py:/admin/status`*
