"""附件上传框架：attachment_menu（file_input / trigger / preview）与回退。

运行：.venv/bin/python -m pytest tests/test_attachments.py -v
"""

from __future__ import annotations

import base64

import pytest

from ai_web2api.config import ProviderConfig
from ai_web2api.core.errors import AttachmentError
from ai_web2api.providers.webchat import WebChatProvider

pytestmark = pytest.mark.asyncio


class _Locator:
    def __init__(self, page: "_Page", sel: str):
        self._page = page
        self._sel = sel

    @property
    def first(self) -> "_Locator":
        return self

    async def count(self) -> int:
        return 1 if self._sel in self._page.present else 0

    async def click(self) -> None:
        self._page.clicks.append(self._sel)

    async def set_input_files(self, paths) -> None:
        self._page.uploads.append((self._sel, list(paths)))

    async def bounding_box(self):
        return {"y": 100}


class _Page:
    def __init__(self, present=()):
        self.present = set(present)
        self.clicks: list[str] = []
        self.uploads: list[tuple[str, list[str]]] = []

    def locator(self, sel: str) -> _Locator:
        return _Locator(self, sel)

    async def wait_for_timeout(self, ms: float) -> None:
        return None

    async def evaluate(self, *a, **k):
        return 0


def _prov(selectors: dict) -> WebChatProvider:
    cfg = ProviderConfig.model_validate(
        {
            "name": "qwen",
            "url": "https://chat.qwen.ai/",
            "models": [{"name": "qwen-max"}],
            "selectors": selectors,
        }
    )
    prov = WebChatProvider(cfg, browser=None)  # type: ignore[arg-type]
    prov.ATTACH_WAIT_SECONDS = 0.05
    prov.ATTACH_INPUT_TIMEOUT = 0.05
    return prov


def _att(name: str = "a.png") -> dict:
    return {"name": name, "data": base64.b64encode(b"hello").decode()}


async def test_uses_attachment_menu_file_input():
    prov = _prov({"attachment_menu": {"file_input": ["input#filesUpload"]}})
    page = _Page(present={"input#filesUpload"})
    await prov._upload_attachments(page, [_att()])
    assert page.uploads and page.uploads[0][0] == "input#filesUpload"


async def test_falls_back_to_upload_input():
    prov = _prov({"upload_input": ["input[type=file]"]})
    page = _Page(present={"input[type=file]"})
    await prov._upload_attachments(page, [_att()])
    assert page.uploads and page.uploads[0][0] == "input[type=file]"


async def test_clicks_trigger_when_configured():
    prov = _prov({"attachment_menu": {"trigger": ["button.plus"], "file_input": ["input#f"]}})
    page = _Page(present={"button.plus", "input#f"})
    await prov._upload_attachments(page, [_att()])
    assert page.clicks == ["button.plus"]  # 先点开菜单
    assert page.uploads and page.uploads[0][0] == "input#f"


async def test_missing_input_raises():
    prov = _prov({})  # 无任何 file input
    page = _Page()
    with pytest.raises(AttachmentError):
        await prov._upload_attachments(page, [_att()])
