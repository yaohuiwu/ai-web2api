"""API options 透传：请求 → 驱动 `_apply_extra_options` / `complete`。

运行：.venv/bin/python -m pytest tests/test_options_passthrough.py -v
"""

from __future__ import annotations

import pytest

from ai_web2api.api.schemas import ChatCompletionRequest
from ai_web2api.config import ProviderConfig
from ai_web2api.providers.webchat import WebChatProvider

pytestmark = pytest.mark.asyncio


async def test_request_schema_accepts_options():
    req = ChatCompletionRequest.model_validate(
        {
            "model": "qwen-max-web",
            "messages": [{"role": "user", "content": "hi"}],
            "options": {"web_search": True},
        }
    )
    assert req.options == {"web_search": True}


class _Capture(WebChatProvider):
    def __init__(self, cfg) -> None:
        super().__init__(cfg, browser=None)  # type: ignore[arg-type]
        self.seen = None

    async def _apply_extra_options(self, page, options: dict) -> None:
        self.seen = options


def _capture() -> _Capture:
    cfg = ProviderConfig.model_validate(
        {"name": "qwen", "url": "https://chat.qwen.ai/", "models": [{"name": "m"}]}
    )
    return _Capture(cfg)


async def test_extra_options_hook_called():
    p = _capture()
    await p._apply_options(
        page=None, mode=None, deep_think=None, search=None, can_set_mode=True,
        options={"web_search": True},
    )
    assert p.seen == {"web_search": True}


async def test_no_options_no_hook():
    p = _capture()
    await p._apply_options(page=None, mode=None, deep_think=None, search=None, can_set_mode=True)
    assert p.seen is None


class _GenStub(WebChatProvider):
    def __init__(self) -> None:
        self.seen = None

    async def generate(
        self,
        messages,
        model,
        *,
        thread_mode=None,
        thread_page=None,
        mode=None,
        deep_think=None,
        search=None,
        attachments=None,
        options=None,
        prompt_override=None,
    ):
        self.seen = options
        if False:
            yield  # pragma: no cover  （使其成为 async generator）


async def test_complete_forwards_options():
    g = _GenStub()
    await g.complete([{"role": "user", "content": "hi"}], "m", options={"x": 2})
    assert g.seen == {"x": 2}
