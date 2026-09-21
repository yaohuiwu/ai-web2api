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
    """附件入口：可选先点 ``trigger`` 打开菜单，再 ``set_input_files(file_input)``。"""

    trigger: Annotated[list[str], BeforeValidator(_norm_selectors)] = []
    file_input: Annotated[list[str], BeforeValidator(_norm_selectors)] = []


class SelectorsConfig(BaseModel):
    # 每个字段都是"候选列表"：按顺序取第一个在页面上匹配的选择器（UI 改版容错）
    input: Annotated[list[str], BeforeValidator(_norm_selectors)] = ["textarea#chat-input"]
    send_button: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 空 = 用回车发送
    response_container: Annotated[list[str], BeforeValidator(_norm_selectors)] = [".ds-markdown"]
    thinking_container: Annotated[list[str], BeforeValidator(_norm_selectors)] = []
    stop_button: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 填了可加快"生成结束"判定
    login_check: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 存在即已登录（空 = 用 input）
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
    # auto 模式：.env 中凭据的键名
    username_env: str = "DEEPSEEK_USERNAME"
    password_env: str = "DEEPSEEK_PASSWORD"
    page: LoginPageSelectors = Field(default_factory=LoginPageSelectors)


class QueueConfig(BaseModel):
    max_size: int = 10
    timeout: float = 60.0


class ModelConfig(BaseModel):
    name: str
    ui_label: str | None = None


class ProviderConfig(BaseModel):
    name: str
    enabled: bool = True
    driver: str = ""                # 驱动类名，空 = 用 name
    url: str
    models: list[ModelConfig]
    selectors: SelectorsConfig = Field(default_factory=SelectorsConfig)
    login: LoginConfig = Field(default_factory=LoginConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    model_aliases: dict[str, str] = {}  # 别名 → 本 provider 的模型名（如 gpt-4 → deepseek-web）
    response_timeout: float = 180.0
    poll_interval: float = 0.2
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
    status_check: bool = True            # 定时状态检测总开关（关掉后后台不再访问页面）
    status_check_headless: bool = True   # 定时检测用独立 headless 浏览器（不弹窗口，默认开）


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    api_keys: list[str] = Field(default_factory=list)  # 空 = 不鉴权（本地使用）
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
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
