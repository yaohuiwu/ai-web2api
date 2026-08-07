"""配置加载：YAML → Pydantic 校验。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, Field, field_validator


def _norm_selectors(v: Any) -> list[str]:
    """str → [str]；None → []。所有选择器统一为候选列表，驱动取第一个匹配的。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [s for s in v if s and isinstance(s, str)]


class SelectorsConfig(BaseModel):
    # 每个字段都是"候选列表"：按顺序取第一个在页面上匹配的选择器（UI 改版容错）
    input: Annotated[list[str], BeforeValidator(_norm_selectors)] = ["textarea#chat-input"]
    send_button: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 空 = 用回车发送
    response_container: Annotated[list[str], BeforeValidator(_norm_selectors)] = [".ds-markdown"]
    thinking_container: Annotated[list[str], BeforeValidator(_norm_selectors)] = []
    stop_button: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 填了可加快"生成结束"判定
    login_check: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 存在即已登录（空 = 用 input）
    new_chat_button: Annotated[list[str], BeforeValidator(_norm_selectors)] = []  # 每次请求前点"新建对话"（可选）


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
    viewport: dict = Field(default_factory=lambda: {"width": 1440, "height": 900})
    default_timeout: float = 30.0
    login_check_interval: float = 300.0  # 后台刷新登录态间隔（秒）
    state_save_interval: float = 600.0   # 后台定期保存 storage_state 间隔（秒）


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    api_keys: list[str] = Field(default_factory=list)  # 空 = 不鉴权（本地使用）
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    # 会话绑定（thread_id）：空闲回收 TTL / 上限 / 是否并行
    thread_ttl: float = 900.0      # 秒，thread 空闲多久回收（关页面）
    max_threads: int = 8           # 同时活跃 thread 上限，超出 429
    thread_parallel: bool = True   # False = thread 请求也走 provider 全局串行（保守防风控）


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


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(raw)
