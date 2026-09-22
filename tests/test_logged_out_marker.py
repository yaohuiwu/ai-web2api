"""反向登录标记（logged_out）：游客态也有输入框的站点不能误判已登录（豆包实测）。

运行：.venv/bin/python -m pytest tests/test_logged_out_marker.py -v
"""

from __future__ import annotations

import pytest

from ai_web2api.config import ProviderConfig
from ai_web2api.providers.webchat import WebChatProvider


class _Loc:
    def __init__(self, visible: bool) -> None:
        self._visible = visible

    @property
    def first(self):
        return self

    async def is_visible(self) -> bool:
        return self._visible


class _Page:
    def __init__(self, visible_selectors: set[str]) -> None:
        self._visible = visible_selectors

    def locator(self, sel):
        return _Loc(sel in self._visible)

    async def close(self):      # check_login 的 finally 会关页面
        return None


class _Browser:
    default_locale = "zh-CN"

    def __init__(self, page=None) -> None:
        self.page = page

    async def open_page(self, *_a, **_kw):
        return self.page


def _prov(page=None, **selectors) -> WebChatProvider:
    cfg = ProviderConfig.model_validate(
        {
            "name": "doubao",
            "url": "https://www.doubao.com/chat",
            "models": [{"name": "doubao-web"}],
            "selectors": selectors,
        }
    )
    return WebChatProvider(cfg, _Browser(page))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_logged_out_visible():
    prov = _prov(logged_out=[".login-button"])
    assert await prov.logged_out_visible(_Page({".login-button"})) is True
    assert await prov.logged_out_visible(_Page(set())) is False


@pytest.mark.asyncio
async def test_check_login_false_when_marker_visible(monkeypatch):
    """命中 input（游客态也有）但存在未登录标记 → 判定未登录（这是 CLI 误退出的根因）。"""
    prov = _prov(_Page({".composer", ".login-button"}), input=[".composer"], logged_out=[".login-button"])

    async def fake_goto_ready(*_a, **_kw):
        return ".composer"

    monkeypatch.setattr(type(prov), "_goto_ready", fake_goto_ready)
    assert await prov.check_login() is False


@pytest.mark.asyncio
async def test_check_login_true_without_marker(monkeypatch):
    prov = _prov(_Page({".composer"}), input=[".composer"], logged_out=[".login-button"])

    async def fake_goto_ready(*_a, **_kw):
        return ".composer"

    monkeypatch.setattr(type(prov), "_goto_ready", fake_goto_ready)
    assert await prov.check_login() is True


def test_doubao_config_has_marker_and_send_button():
    from pathlib import Path

    from ai_web2api.config import load_config

    root = Path(__file__).resolve().parent.parent
    doubao = next(p for p in load_config(root / "config.yaml").providers if p.name == "doubao")
    assert any("login-btn" in s for s in doubao.selectors.logged_out), "豆包必须靠反向标记判定登录"
    assert ".send-btn-wrapper" in doubao.selectors.send_button, "实测到的发送控件别删"
