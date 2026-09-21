"""发送提示词：多行内容必须用 Shift+Enter 换行，不能用 Enter（会被当成“发送”截断）。

运行：.venv/bin/python -m pytest tests/test_send_newlines.py -v
"""

from __future__ import annotations

import pytest

from ai_web2api.config import ProviderConfig
from ai_web2api.providers.webchat import WebChatProvider


class _StubBrowser:
    default_locale = "zh-CN"


class _Input:
    def __init__(self, rec: list) -> None:
        self.rec = rec

    async def press_sequentially(self, text: str, delay: float = 0) -> None:
        self.rec.append(("type", text))


class _Keyboard:
    def __init__(self, rec: list) -> None:
        self.rec = rec

    async def press(self, key: str) -> None:
        self.rec.append(("key", key))


class _Page:
    def __init__(self) -> None:
        self.rec: list = []
        self.keyboard = _Keyboard(self.rec)


def _prov() -> WebChatProvider:
    cfg = ProviderConfig.model_validate(
        {"name": "chatgpt", "url": "https://chatgpt.com/", "models": [{"name": "gpt-5-web"}]}
    )
    return WebChatProvider(cfg, _StubBrowser())  # type: ignore[arg-type]



@pytest.mark.asyncio
async def test_newlines_use_shift_enter():
    prov, page = _prov(), _Page()
    inp = _Input(page.rec)
    await prov._type_prompt_human(page, inp, "line1\nline2\n\nline4")

    typed = [t for kind, t in page.rec if kind == "type"]
    keys = [k for kind, k in page.rec if kind == "key"]
    assert typed == ["line1", "line2", "line4"]  # 空行不输入字符
    assert keys == ["Shift+Enter", "Shift+Enter", "Shift+Enter"]  # 3 个换行
    assert "Enter" not in keys  # 绝不裸 Enter（会提前发送）


@pytest.mark.asyncio
async def test_crlf_normalized():
    prov, page = _prov(), _Page()
    await prov._type_prompt_human(page, _Input(page.rec), "a\r\nb\rc")
    assert [t for kind, t in page.rec if kind == "type"] == ["a", "b", "c"]
    assert [k for kind, k in page.rec if kind == "key"] == ["Shift+Enter", "Shift+Enter"]


@pytest.mark.asyncio
async def test_single_line_has_no_newline_key():
    prov, page = _prov(), _Page()
    await prov._type_prompt_human(page, _Input(page.rec), "可用工具:[{...}]")
    assert [k for kind, k in page.rec if kind == "key"] == []
