# 智谱清言（chatglm.cn）接入说明（**受 WAF 阻挡，默认禁用**）

> 状态：**默认 `enabled: false`**，且**当前不具备纯自动化条件**。
> 驱动：`src/ai_web2api/providers/glm.py`（`GlmProvider`，纯配置驱动）。登录：**仅手动**（无密码登录）。

## 1. 实测结论（2026-09）

| 项 | 结果 |
|---|---|
| 站点 | `https://chatglm.cn/main/alltoolsdetail?lang=zh` |
| **访问前置** | **阿里云 WAF 滑动验证**：页面标题即「滑动验证页面」，正文「访问验证 别离开，为了更好的访问体验，请进行验证，通过后即可继续访问网页 请按住滑块，拖动到最右边」 |
| headless | ❌ 落到验证页，无输入框 |
| **headful（Xvfb）** | ❌ **同样落到验证页**（`editable=0`）→ 不是"headless 特征"问题，DOM 自动化**无法通过** |
| 登录方式 | 无密码（手机号验证码/扫码）→ `login.mode: manual` |
| 会话 URL | 形态未确认（`alltoolsdetail?…`）→ 暂不启用 thread 恢复（`session_url_pattern = None`） |

> 本项目原则是**纯 DOM 自动化、不逆向内部接口**；滑块属于站点反爬机制，不做破解/打码接入。

## 2. 如果仍要使用：可行路径与代价

1. 在**有显示器的宿主**上执行 `./scripts/login.sh glm`：人工拖滑块 + 完成登录；
2. 脚本会把 `storage_state`（**含 WAF cookie**）导入服务；容器与宿主同一出口 IP，理论上可复用；
3. 然后按 `docs/PROVIDER_DOUBAO.md` 的第 2 节校准 `input` / `send_button` / `response_container` / `login_check`；
4. **风险**：阿里云 WAF 的验证 cookie 存活时间通常很短（分钟~小时级，且与 IP/指纹绑定），
   一旦失效就要人工重新过滑块 → **不适合长期无人值守**。建议只在"临时用一下"时开启。

## 3. 结论

- 想要稳定可用，建议改用**官方 API**（智谱开放平台）而不是网页自动化；
- 若坚持网页方式，请把本 provider 当作**实验性、需人工保活**的能力，保持 `enabled: false`，用时再开。
