"""开关点击的读回校验：漏点/读错 → 自动重试；仍不一致 → WARNING，不再静默。

背景：playground 勾了"深度思考"却像没生效（旧实现点击后不校验，UI 吞掉点击时
静默返回）。本文件用最小 stub page 直接驱动 DeepSeekProvider._set_toggle，
不需要真浏览器。

运行：.venv/bin/python -m pytest tests/test_toggle_verify.py -v
"""

from __future__ import annotations

import logging

import pytest

from ai_web2api.config import ProviderConfig
from ai_web2api.providers.deepseek import DeepSeekProvider

pytestmark = pytest.mark.asyncio  # 本文件全为 async 用例（pytest-asyncio strict 模式）

TOGGLE_SEL = 'div.ds-toggle-button:has-text("深度思考")'
CHECKED_SEL = ".ds-toggle-button--selected"


class StubLocator:
    def __init__(self, page: "StubPage", selector: str):
        self.page = page
        self.selector = selector

    async def count(self) -> int:
        page = self.page
        if self.selector == page.toggle_sel:
            return 1
        if self.selector == page.checked_sel:
            return 1 if page.on else 0
        return 0

    @property
    def first(self) -> "StubLocator":
        return self

    async def evaluate(self, js: str, arg: str) -> bool:
        page = self.page
        if self.selector == page.toggle_sel and arg == page.checked_sel:
            if page.misread_next:
                page.misread_next = False
                return not page.on  # 模拟状态读错一次
            return page.on
        return False

    async def click(self) -> None:
        page = self.page
        assert self.selector == page.toggle_sel, f"意外点击 {self.selector}"
        page.clicks += 1
        if page.click_raises:
            raise RuntimeError("element is not clickable (遮罩)")
        if not page.swallow_click:
            page.on = not page.on


class StubPage:
    def __init__(self, *, on: bool = False, swallow_click: bool = False,
                 click_raises: bool = False, toggle_sel: str = TOGGLE_SEL,
                 checked_sel: str = CHECKED_SEL):
        self.on = on
        self.swallow_click = swallow_click
        self.click_raises = click_raises
        self.toggle_sel = toggle_sel
        self.checked_sel = checked_sel
        self.misread_next = False
        self.clicks = 0

    def locator(self, selector: str) -> StubLocator:
        return StubLocator(self, selector)

    async def wait_for_timeout(self, _ms: int) -> None:
        return None


def _provider(toggle_cands: list[str] | None = None) -> DeepSeekProvider:
    cfg = ProviderConfig(
        name="deepseek",
        driver="deepseek",
        url="https://chat.deepseek.com/",
        models=[],
        selectors={
            "toggle_button": {"deep_think": toggle_cands if toggle_cands is not None else [TOGGLE_SEL]},
            "toggle_checked": [CHECKED_SEL],
        },
    )
    return DeepSeekProvider(cfg, browser=None)  # type: ignore[arg-type]  # _set_toggle 不用浏览器


async def test_already_on_skips_click() -> None:
    """已是目标态 → 不点击，返回 True。"""
    page = StubPage(on=True)
    assert await _provider()._set_toggle(page, "deep_think", [TOGGLE_SEL], True) is True
    assert page.clicks == 0 and page.on is True


async def test_clicks_once_to_reach_target() -> None:
    """关 → 开：一次点击并读回确认。"""
    page = StubPage(on=False)
    assert await _provider()._set_toggle(page, "deep_think", [TOGGLE_SEL], True) is True
    assert (page.clicks, page.on) == (1, True)


async def test_swallowed_click_is_retried_then_warned(caplog: pytest.LogCaptureFixture) -> None:
    """点击被 UI 吞掉：重试一次；仍不一致 → 返回 False + WARNING（不再静默）。"""
    page = StubPage(on=False, swallow_click=True)
    with caplog.at_level(logging.WARNING):
        ok = await _provider()._set_toggle(page, "deep_think", [TOGGLE_SEL], True)
    assert ok is False
    assert page.clicks == 2, "应重试一次"
    assert page.on is False
    assert any("未生效" in r.message for r in caplog.records), caplog.text


async def test_wrong_state_read_is_corrected_by_retry() -> None:
    """状态读错（页面实际已开，读到关）：重试后回到正确目标态。"""
    page = StubPage(on=True)
    page.misread_next = True  # 第一次读回撒谎
    assert await _provider()._set_toggle(page, "deep_think", [TOGGLE_SEL], True) is True
    assert (page.clicks, page.on) == (2, True)


async def test_click_error_is_surfaced(caplog: pytest.LogCaptureFixture) -> None:
    """点击抛错（元素被遮罩）→ 返回 False + WARNING，不向上抛。"""
    page = StubPage(on=False, click_raises=True)
    with caplog.at_level(logging.WARNING):
        ok = await _provider()._set_toggle(page, "deep_think", [TOGGLE_SEL], True)
    assert ok is False
    assert any("点击失败" in r.message for r in caplog.records), caplog.text


async def test_missing_selector_on_page_returns_false() -> None:
    """候选选择器在页面上不存在（UI 改版/未渲染）→ False 且不点击。"""
    page = StubPage(on=False, toggle_sel="div.other")
    assert await _provider()._set_toggle(page, "deep_think", [TOGGLE_SEL], True) is False
    assert page.clicks == 0


async def test_empty_candidates_returns_false() -> None:
    """配置里没有该开关 → False（该 UI 没有这个开关）。"""
    page = StubPage(on=False)
    assert await _provider(toggle_cands=[])._set_toggle(page, "deep_think", [], True) is False
    assert page.clicks == 0


async def test_apply_options_reports_applied_state() -> None:
    """_apply_options 走通：显式 deep_think=on/search=off 都落到页面。"""
    page = StubPage(on=False)
    prov = _provider()
    prov.cfg.selectors.toggle_button["search"] = ['div.ds-toggle-button:has-text("智能搜索")']
    await prov._apply_options(page, None, True, False, can_set_mode=True)
    assert page.on is True and page.clicks == 1
