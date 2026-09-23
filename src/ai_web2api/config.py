"""配置加载：YAML → Pydantic 校验 → 环境变量覆盖。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, Field, field_validator

logger = logging.getLogger(__name__)

# 环境变量里的布尔字面量（.env 常见写法）
_TRUTHY = {"1", "true", "yes", "on", "y", "t"}
_FALSY = {"0", "false", "no", "off", "n", "f"}


def _parse_bool(raw: str) -> bool:
    v = raw.strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    raise ValueError(f"不是布尔值: {raw!r}")


def _norm_selectors(v: Any) -> list[str]:
    """str → [str]；None → []。所有选择器统一为候选列表，驱动取第一个匹配的。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [s for s in v if s and isinstance(s, str)]


class MenuConfig(BaseModel):
    """下拉菜单（Ant Design 风格）：点 ``trigger`` 展开，再点 ``option``。

    ``option`` 里可用 ``{label}`` 占位（如 ``div[role=option]:has-text("{label}")``），
    运行时用目标文案（模型 ``ui_label`` / 模式 labels）替换。``current`` 可选，
    用于读回当前值、已是目标则跳过。
    """

    trigger: Annotated[list[str], BeforeValidator(_norm_selectors)] = []
    option: Annotated[list[str], BeforeValidator(_norm_selectors)] = []
    current: Annotated[list[str], BeforeValidator(_norm_selectors)] = []


class ModeMenuConfig(MenuConfig):
    """模式下拉：API ``mode`` → UI 文案（``labels``）。"""

    labels: dict[str, str] = {}


class AttachmentMenuConfig(BaseModel):
    """附件入口：可选先点 ``trigger`` 打开菜单，再 ``set_input_files(file_input)``。

    - ``file_input``：文件 input（如 Qwen ``input#filesUpload``）；空则用
      ``selectors.upload_input``。
    - ``trigger``：需要先展开菜单才渲染 input 时配置；Qwen 的 input 已在 DOM，可留空。
    - ``preview``：上传完成的判定选择器（空则用通用 blob 图片计数）。
    """

    trigger: Annotated[list[str], BeforeValidator(_norm_selectors)] = []
    item: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 需先点菜单项才弹文件选择器（如 Qwen「上传附件」）
    file_input: Annotated[list[str], BeforeValidator(_norm_selectors)] = []
    preview: Annotated[list[str], BeforeValidator(_norm_selectors)] = []


class SelectorsConfig(BaseModel):
    # 每个字段都是"候选列表"：按顺序取第一个在页面上匹配的选择器（UI 改版容错）
    input: Annotated[list[str], BeforeValidator(_norm_selectors)] = ["textarea#chat-input"]
    send_button: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 空 = 用回车发送
    # 输入框是 contenteditable / React（如 ChatGPT）时用逐字输入——fill() 可能不生效
    type_prompt: bool = False
    # 正文流式策略：True=逐字 diff（默认）；False=缓冲后一次性发（ChatGPT 等 markdown 重渲染严重、diff 会丢内容）
    stream_content: bool = True
    # 一次回答被拆成多条消息/多个容器时（如 Kimi 工具调用），把检测到的新容器**全部拼接**，
    # 否则只会拿到第一段（默认关，仅需要的站点开）
    response_all_new: bool = False
    # 会遮挡输入的弹窗/横幅：出现即点掉（如 Kimi 的「和Kimi聊天的人太多了」提示）
    dismiss_button: list[str] = Field(default_factory=list)
    # 出现 = 站点在提示繁忙/排队/限制：用于给出明确错误，而不是干等超时
    busy_hint: list[str] = Field(default_factory=list)
    # 把本轮**新出现的 iframe**（交互组件/小部件，如 Kimi 的 kimi-canvas 地图）
    # 作为「链接 + 可见文本」附到正文末尾（跨域 frame 也能读；默认关）
    include_frames: bool = False
    # 交互组件（iframe）捕获：none(默认) | png | html | both ——
    # html 仍进 Playground 的 sandbox iframe（可交互），png 作保真兜底
    widget_capture: Literal["none", "png", "html", "both"] = "none"
    response_container: Annotated[list[str], BeforeValidator(_norm_selectors)] = [".ds-markdown"]
    thinking_container: Annotated[list[str], BeforeValidator(_norm_selectors)] = []
    stop_button: Annotated[list[str], BeforeValidator(_norm_selectors)] = []
    # 「消息已完成」工具栏（复制/点赞/朗读…）选择器：**该条回复的工具栏出现**即视为
    # 站点认为这条消息写完了（实测 ChatGPT：工具栏出现 == 停止按钮消失 == 本轮结束）。
    # 只在"最后一个正文容器所在的消息块"里检查，不会命中历史消息；隐藏（仅 hover）不算。
    # 适合**没有可用停止按钮**的站点（如豆包）作为结束信号。
    done_toolbar: Annotated[list[str], BeforeValidator(_norm_selectors)] = []
    # 「预览流」：缓冲模式（stream_content: false）下**边生成边把新追加的文字发出去**（实时感）。
    # 与 true 的流式模式区别：只发**前缀追加**的增量（非前缀的 DOM 重渲染改写一律丢弃，防脏数据），
    # 定稿时再按最长公共前缀补发尾巴 → 既不脏也不丢。**定稿条件完全不受影响**（防截断安全网照旧）。
    preview_stream: bool = False
    # 攒够这么多字才开始发（避免把开场动画/自己的提问当正文误发）
    preview_min_chars: int = 12
    # 停止按钮**消失后**，再要求 N 拍无变化才定稿（双确认）：
    # 否则可能在最后一拍渲染完成前就取文本 → 长回答/表格被截断（实测 Kimi/豆包/GLM）
    stop_settle_polls: int = 2  # 填了可加快"生成结束"判定
    login_check: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 存在即已登录（空 = 用 input）
    # 反向标记：**可见即视为未登录**；用于「游客态也有输入框」的站点（如豆包），
    # 否则 login_check 回退到 input 会把游客误判成已登录（手动登录命令也会直接退出）。
    logged_out: Annotated[list[str], BeforeValidator(_norm_selectors)] = []
    new_chat_button: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 每次请求前点"新建对话"（可选）

    # 模式选择（radiogroup）：API mode 值 → 候选列表（旧版 UI：快速/专家/识图）。
    # 留空 = 该 provider 的 UI 已无模式区（新版 DeepSeek 三模式合一），
    # 此时 mode 值由驱动翻译成开关组合（见 DeepSeekProvider.MODE_PRESETS）。
    mode_button: dict[str, list[str]] = {}
    mode_checked: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 判断当前选中的 radio（如 div[role=radio][aria-checked="true"]）

    # 开关（toggle，如深度思考/智能搜索）：API 字段名 → 候选列表
    toggle_button: dict[str, list[str]] = {}
    toggle_checked: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 判断开关已开
    upload_input: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 附件上传入口（input[type=file] 等）

    # 下拉菜单形态（可选）：模型选择 / 模式选择 / 附件入口
    model_menu: MenuConfig = Field(default_factory=MenuConfig)
    mode_menu: ModeMenuConfig = Field(default_factory=ModeMenuConfig)
    attachment_menu: AttachmentMenuConfig = Field(default_factory=AttachmentMenuConfig)


class LoginPageSelectors(BaseModel):
    """登录页选择器（候选列表，取第一个匹配的）。DeepSeek 2026-08 实测值见 config.yaml。"""

    password_tab: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 切"密码登录"tab（默认可能是验证码 tab）
    splash: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 加载遮罩：点击前等它消失（否则拦截点击）
    username: Annotated[list[str], BeforeValidator(_norm_selectors)] = [
        "input[placeholder*=\"手机号\"]",
        "input[type=text]",
    ]
    password: Annotated[list[str], BeforeValidator(_norm_selectors)] = [
        "input[type=password]"
    ]
    submit: Annotated[list[str], BeforeValidator(_norm_selectors)] = [
        "div.ds-button--primary",
        "button[type=submit]",
    ]


class LoginConfig(BaseModel):
    mode: Literal["manual", "cookies", "auto"] = "manual"
    hint: str = ""
    url: str | None = None  # 登录页 URL（与聊天页不同时用，如 Qwen 的 /auth；空 = 用 provider.url）
    retries: int = 3  # 自动登录失败时的重试次数（网页登录首发可能静默无效）
    # 登录成功的**真实标记**（可选）：`./scripts/login.sh` 用它自动确认"真的登录成功"。
    # 为空则该 provider 不做自动检测，只接受用户按回车确认 ——
    # 避免游客态站点（Gemini 游客可用）把输入框误判成登录成功。
    detect: list[str] = Field(default_factory=list)
    # 认证有效期：声明哪些 cookie 代表登录态（glob，大小写不敏感）。
    # 不配 = 无法判断（UI 显示「未知」）；切勿用「最早到期的 cookie」（会命中 WAF/偏好 cookie 误报）
    auth_cookies: list[str] = Field(default_factory=list)
    session_ttl_days: float | None = None  # 命中 cookie 全为会话型时，按 state.json mtime + 此 TTL 估算（可选）
    # localStorage 里的 token（glob）：Kimi/DeepSeek 的 access/refresh token 存在这里且是 JWT，
    # 配了就会读其 exp 参与「认证有效期」展示与提醒（如 refresh_token）
    auth_local_storage: list[str] = Field(default_factory=list)
    expiry_warn_days: float | None = None  # 覆盖 browser.auth_expiry_warn_days（手动认证可设更早）
    # auto 模式：.env 中凭据的键名
    username_env: str = "DEEPSEEK_USERNAME"
    password_env: str = "DEEPSEEK_PASSWORD"
    page: LoginPageSelectors = Field(default_factory=LoginPageSelectors)


class QueueConfig(BaseModel):
    max_size: int = 10
    timeout: float = 60.0


class NetworkConfig(BaseModel):
    """网络抓取（XHR/SSE）：按 ``url_pattern`` 生成页面级监听脚本。

    - ``capture``：总开关；关 / 未配 ``url_pattern`` → 不注入，直接走 DOM 轮询。
    - ``url_pattern``：匹配要监听的 XHR URL 的正则（如 DeepSeek ``/api/v0/chat/completion``）。
    - ``grace_seconds``：首次事件宽限期（空 = 自动 ``min(max(6, timeout*0.2), 20)``）。
    """

    capture: bool = True
    # 观测型旁听（**不解析内容**）：用 Playwright 的响应事件记录匹配请求的
    # 状态码 / 响应头时间 / 结束时间。用于判断"卡住"是站点排队限流、长思考，还是流未结束。
    observe: bool = False
    url_pattern: str | None = None
    grace_seconds: float | None = None


class ModelConfig(BaseModel):
    name: str
    ui_label: str | None = None


class ProviderConfig(BaseModel):
    name: str
    enabled: bool = True
    driver: str = ""                # 驱动类名，空 = 用 name
    url: str
    locale: str | None = None       # 覆盖全局 browser.locale（多 provider 语言不同）
    session_url: str | None = None  # thread 恢复 URL 模板：{base}（无尾斜杠） / {id}
    models: list[ModelConfig]
    selectors: SelectorsConfig = Field(default_factory=SelectorsConfig)
    login: LoginConfig = Field(default_factory=LoginConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    model_aliases: dict[str, str] = {}  # 别名 → 本 provider 的模型名（如 gpt-4 → deepseek-web）
    response_timeout: float = 180.0
    # 发送确认窗口（秒）：这么久内既无新容器也无停止按钮 → 判定消息没真正发出，
    # 重发一次并快速失败（0 = 关闭）。避免被静默丢弃时死等满 response_timeout。
    send_confirm_timeout: float = 20.0
    poll_interval: float = 0.2
    # 抓取模式：poll（默认，定时轮询）| observer（**事件驱动**：页面 DOM 一变化就唤醒提取，
    # 并自动重绑到最新的回复容器；任何异常/超时都回退 poll，行为与今天一致）
    capture_mode: str = "poll"
    observer_debounce_ms: int = 100      # 页面侧节流：同一帧内的多次变化合并成一次唤醒
    observer_min_interval_ms: int = 150  # 最小提取间隔：重渲染风暴时也不至于疯狂提取
    observer_grace_seconds: float = 8.0  # 这么久无事件且无正文 → 判定注入失效 → 回退 poll
    stable_polls: int = 12          # 连续多少次轮询无变化判定"结束"（稳定兜底）
    min_wait_before_stable: float = 8.0  # 稳定判定生效前的最短等待（防思考→正文间隙误判）
    thread_busy_timeout: float = 20.0  # resume 时发送后无新容器即判定页面忙（上一请求未完成）


class BrowserConfig(BaseModel):
    headless: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    # 页面语言：DeepSeek 按 Accept-Language 决定 UI 语言，选择器文案（深度思考/开启新对话、
    # 登录页"密码登录"等）全是中文 → 必须固定 zh-CN，否则 Playwright 默认 en-US 时全部失配。
    locale: str = "zh-CN"
    viewport: dict = Field(default_factory=lambda: {"width": 1440, "height": 900})
    default_timeout: float = 30.0
    login_check_interval: float = 300.0  # 后台刷新登录态间隔（秒）
    state_expiry_margin: float = 86400.0  # 登录态剩余有效期低于该值才落盘 state.json（秒）；未过期不重复写
    auth_expiry_warn_days: float = 3.0    # 「认证即将过期」默认预警阈值（天）；provider 可用 login.expiry_warn_days 覆盖
    status_check: bool = True            # 定时状态检测总开关（关掉后后台不再访问页面）
    # 诊断用：>0 时给 Chromium 传 ``--remote-debugging-port``，可用 Playwright
    # ``connect_over_cdp`` **连到服务正在用的页面**查看真实 DOM/时序（省得新开会话触发风控）。
    # 默认 0 关闭；仅在容器内可用（docker-compose 未把该端口 publish 出去）。
    debug_port: int = 0
    status_check_headless: bool = True   # 定时检测用独立 headless 浏览器（不弹窗口，默认开）


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    api_keys: list[str] = Field(default_factory=list)  # 空 = 不鉴权（本地使用）
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    # 常见 OpenAI 模型名（gpt-4 等）兜底别名挂给哪个 provider；空 = 第一个启用的 provider
    default_provider: str | None = None
    # Function Calling 总开关；false = 完全忽略 tools（不注入工具提示，避免触发网页端风控）
    function_calling: bool = True
    # 仓库地址：UI 顶部显示 GitHub Star 入口 + 星标数（空 = 不显示；计数由后端带缓存拉取）
    repo_url: str | None = None
    # 实时画面（Live View，见 docs/LIVE_VIEW.md）：**只读**直播，接口无鉴权 → 对外暴露请自加反代鉴权
    live_view: bool = True       # 总开关；false = /admin/{p}/screen* 一律 403
    live_control: bool = False   # 预留：输入注入（P2），默认关
    live_fps: float = 5.0        # 默认帧率（可被 ?fps= 覆盖；生成中自动降到 1）
    live_quality: int = 50       # 默认 JPEG 质量（可被 ?quality= 覆盖）
    # 会话绑定（thread_id）：空闲回收 TTL / 上限 / 是否并行 / 是否持久化
    thread_ttl: float = 900.0      # 秒，thread 空闲多久回收（关页面）
    max_threads: int = 8           # 同时活跃 thread 上限，超出 429
    thread_parallel: bool = True   # False = thread 请求也走 provider 全局串行（保守防风控）
    thread_persist: bool = True    # thread_id → provider 会话 URL id 落盘，重启后 goto 恢复


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    providers: list[ProviderConfig] = Field(default_factory=list)
    profiles_dir: Path = Path("profiles")

    @field_validator("providers", mode="before")
    @classmethod
    def _normalize_providers(cls, v: Any) -> Any:
        """兼容两种 YAML 写法：list 形式或 dict 形式（key 为 provider 名）。"""
        if isinstance(v, dict):
            out = []
            for name, cfg in v.items():
                if isinstance(cfg, dict) and "name" not in cfg:
                    cfg = {"name": name, **cfg}
                out.append(cfg)
            return out
        return v


def _apply_headless_override(cfg: AppConfig) -> AppConfig:
    """headless 开关：``WEB2API_HEADLESS`` > ``<PROVIDER>_HEADLESS`` > config.yaml。"""
    candidates = ["WEB2API_HEADLESS"] + [
        f"{p.name.upper()}_HEADLESS" for p in cfg.providers
    ]
    for name in candidates:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            continue
        try:
            val = _parse_bool(raw)
        except ValueError:
            logger.warning("环境变量 %s=%r 不是布尔值，忽略", name, raw)
            continue
        if val != cfg.browser.headless:
            logger.info(
                "browser.headless: %s → %s（环境变量 %s）", cfg.browser.headless, val, name
            )
        else:
            logger.info("browser.headless=%s（环境变量 %s）", val, name)
        return cfg.model_copy(
            update={"browser": cfg.browser.model_copy(update={"headless": val})}
        )
    logger.info(
        "browser.headless=%s（来自 config.yaml；未设置 WEB2API_HEADLESS / <PROVIDER>_HEADLESS）",
        cfg.browser.headless,
    )
    return cfg


def apply_env_overrides(cfg: AppConfig) -> AppConfig:
    """用环境变量覆盖 YAML 配置（.env 里配的开关必须真的生效）。

    支持的覆盖（与 ``.env.example`` 一一对应）：

    - ``WEB2API_HEADLESS`` > ``<PROVIDER>_HEADLESS``（如 ``DEEPSEEK_HEADLESS``）
      → ``browser.headless``
    - ``WEB2API_API_KEY`` → ``server.api_keys``（支持逗号分隔多个 key）

    host/port/profiles_dir 等其余项以 ``config.yaml`` 为准：``.env`` 会被自动加载，
    若 env 也参与覆盖，则“换个 config 文件启动”（如 ``AI_WEB2API_CONFIG=config.fake.yaml``）
    仍会被 ``.env`` 里的端口霸占，反而失去可预测性。Docker 部署直接挂 config.yaml。

    背景：``.env.example`` 一直文档化这些变量，但配置加载只读 YAML，
    导致设了无头仍弹窗口。
    """
    cfg = _apply_headless_override(cfg)

    key = os.environ.get("WEB2API_API_KEY", "").strip()
    if key:
        # 逗号分隔多 key；保留 YAML 里已配的 key（取并集，去重保序）
        keys = [k.strip() for k in key.split(",") if k.strip()]
        merged = list(dict.fromkeys([*cfg.server.api_keys, *keys]))
        logger.info("server.api_keys 被环境变量 WEB2API_API_KEY 覆盖（%d key）", len(merged))
        cfg = cfg.model_copy(
            update={"server": cfg.server.model_copy(update={"api_keys": merged})}
        )
    return cfg


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = AppConfig.model_validate(raw)
    _resolve_relative_file_urls(cfg, p.resolve().parent)
    return apply_env_overrides(cfg)


def _resolve_relative_file_urls(cfg: AppConfig, base_dir: Path) -> None:
    """把 ``file://./xxx`` 相对 URL 解析成绝对路径（相对配置文件所在目录）。

    自测配置需指向仓库内的 ``tests/fake_chat.html``：写死绝对路径换机器就废，
    且 Docker 构建上下文只挂当前目录 → 用相对配置文件的写法。
    """
    for provider in cfg.providers:
        url = provider.url
        if not url.startswith("file://"):
            continue
        rel = url[len("file://"):]
        if rel.startswith("./") or not rel.startswith("/"):
            target = (base_dir / rel).resolve()
            provider.url = f"file://{target}"
