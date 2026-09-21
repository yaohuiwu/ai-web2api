"""下拉菜单选项（模型 / 模式）单元测试。

用最小 stub page 驱动 ``WebChatProvider._apply_model`` / ``_apply_options``：
不依赖浏览器，验证“点开 trigger → 点 option → 已是目标则跳过”。
运行：.venv/bin/python -m pytest tests/test_menu_options.py -v
"""

from __future__ import annotations

import pytest

from ai_web2api.config import ProviderConfig
from ai_web2api.providers.webchat import WebChatProvider

pytestmark = pytest.mark.asyncio  # 本文件全为 async 用例

TRIGGER = "span.ant-dropdown-trigger"
OPTION = 'div[role=option]:has-text("{label}")'


class StubLocator:
    def __init__(self, page: "StubPage", sel: str):
        self._page = page
        self._sel = sel

    @property
    def first(self) -> "StubLocator":
        return self

    async def count(self) -> int:
        return 1 if self._sel in self._page.visible else 0

    async def click(self) -> None:
        self._page.clicks.append(self._sel)

    async def inner_text(self) -> str:
        return self._page.values.get(self._sel, "")


class StubPage:
    def __init__(self, visible=(), values=None):
        self.visible = set(visible)
        self.values = dict(values or {})
        self.clicks: list[str] = []
        self.keys: list[str] = []

    def locator(self, sel: str) -> StubLocator:
        return StubLocator(self, sel)

    async def wait_for_timeout(self, ms: float) -> None:
        return None

    @property
    def keyboard(self) -> "StubPage":
        return self

    async def press(self, key: str) -> None:
        self.keys.append(key)


def _provider(selectors: dict) -> WebChatProvider:
    cfg = ProviderConfig.model_validate(
        {
            "name": "qwen",
            "url": "https://chat.qwen.ai/",
            "models": [{"name": "qwen-max", "ui_label": "Qwen3.8-Max"}],
            "selectors": selectors,
        }
    )
    return WebChatProvider(cfg, browser=None)  # type: ignore[arg-type]


async def test_menu_config_parses():
    cfg = ProviderConfig.model_validate(
        {
            "name": "qwen",
            "url": "https://chat.qwen.ai/",
            "models": [{"name": "qwen-max", "ui_label": "Qwen3.8-Max"}],
            "selectors": {
                "model_menu": {"trigger": [TRIGGER], "option": [OPTION]},
                "mode_menu": {
                    "trigger": [".t"],
                    "option": ['[role=option]:has-text("{label}")'],
                    "labels": {"auto": "自动", "thinking": "思考", "fast": "快速"},
                },
                "attachment_menu": {"file_input": ["input#filesUpload"]},
            },
        }
    )
    sel = cfg.selectors
    assert sel.model_menu.trigger == [TRIGGER]
    assert sel.model_menu.option == [OPTION]
    assert sel.mode_menu.labels["thinking"] == "思考"
    assert sel.attachment_menu.file_input == ["input#filesUpload"]


async def test_apply_model_clicks_trigger_then_option():
    prov = _provider({"model_menu": {"trigger": [TRIGGER], "option": [OPTION]}})
    page = StubPage(visible={TRIGGER, 'div[role=option]:has-text("Qwen3.8-Max")'})
    await prov._apply_model(page, "qwen-max")
    assert page.clicks == [TRIGGER, 'div[role=option]:has-text("Qwen3.8-Max")']


async def test_apply_model_skipped_on_resume():
    prov = _provider({"model_menu": {"trigger": [TRIGGER], "option": [OPTION]}})
    page = StubPage(visible={TRIGGER, 'div[role=option]:has-text("Qwen3.8-Max")'})
    await prov._apply_model(page, "qwen-max", can_set=False)
    assert page.clicks == []


async def test_mode_menu_already_active_skips_click():
    prov = _provider(
        {
            "mode_menu": {
                "trigger": [".t"],
                "option": ['[role=option]:has-text("{label}")'],
                "current": [".cur"],
                "labels": {"thinking": "思考"},
            }
        }
    )
    page = StubPage(visible={".t", ".cur"}, values={".cur": "思考"})
    await prov._apply_options(page, "thinking", None, None, can_set_mode=True)
    assert page.clicks == []


async def test_mode_menu_selects_when_differs():
    prov = _provider(
        {
            "mode_menu": {
                "trigger": [".t"],
                "option": ['[role=option]:has-text("{label}")'],
                "current": [".cur"],
                "labels": {"thinking": "思考"},
            }
        }
    )
    page = StubPage(
        visible={".t", ".cur", '[role=option]:has-text("思考")'},
        values={".cur": "自动"},
    )
    await prov._apply_options(page, "thinking", None, None, can_set_mode=True)
    assert page.clicks == [".t", '[role=option]:has-text("思考")']


async def test_mode_menu_unknown_mode_ignored():
    prov = _provider(
        {"mode_menu": {"trigger": [".t"], "option": ['[role=option]:has-text("{label}")'], "labels": {"auto": "自动"}}}
    )
    page = StubPage(visible={".t"})
    await prov._apply_options(page, "thinking", None, None, can_set_mode=True)
    assert page.clicks == []  # 无映射 → 忽略


async def test_mode_menu_missing_option_closes_menu():
    prov = _provider(
        {"mode_menu": {"trigger": [".t"], "option": ['[role=option]:has-text("{label}")'], "labels": {"auto": "自动"}}}
    )
    prov.MENU_OPTION_TIMEOUT = 0.05  # 免等 5s
    page = StubPage(visible={".t"})  # option 不存在
    await prov._apply_options(page, "auto", None, None, can_set_mode=True)
    assert page.clicks == [".t"]      # 只点了 trigger
    assert page.keys == ["Escape"]    # 展开后关闭
