"""APIProvider 单元测试。"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_web2api.config import ProviderConfig
from ai_web2api.providers.api_provider import APIProvider, _json_path, resolve_api_key
from ai_web2api.core.errors import ProviderError


# ---------- resolve_api_key ----------

def test_resolve_api_key_plain():
    assert resolve_api_key("sk-abc123") == "sk-abc123"


def test_resolve_api_key_env():
    with patch.dict("os.environ", {"MY_KEY": "sk-env"}):
        assert resolve_api_key("${MY_KEY}") == "sk-env"


def test_resolve_api_key_missing_env():
    assert resolve_api_key("${NO_SUCH_KEY}") is None


def test_resolve_api_key_none():
    assert resolve_api_key(None) is None


# ---------- _json_path ----------

def test_json_path_found():
    obj = {"choices": [{"delta": {"content": "hello"}}]}
    assert _json_path(obj, ["choices", "0", "delta", "content"]) == "hello"


def test_json_path_missing():
    obj = {"choices": []}
    assert _json_path(obj, ["choices", "0", "delta", "content"]) is None


def test_json_path_list_index():
    obj = {"items": ["a", "b"]}
    assert _json_path(obj, ["items", "1"]) == "b"


# ---------- APIProvider ----------

def _cfg(**overrides) -> ProviderConfig:
    base = {
        "name": "test-api",
        "enabled": True,
        "driver": "api",
        "url": "https://api.example.com/v1/chat/completions",
        "api_key": "sk-test",
        "models": [{"name": "test-model"}],
    }
    base.update(overrides)
    return ProviderConfig.model_validate(base)


async def _async_iter(items):
    for item in items:
        yield item


def _make_response(status, content_lines, text=""):
    """构造 mock response（MagicMock + async context manager）。"""
    resp = MagicMock()
    resp.status = status
    resp.content = _async_iter(content_lines)
    resp.text = AsyncMock(return_value=text)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.mark.asyncio
async def test_generate_yields_content():
    """SSE 流正确解析 content。"""
    cfg = _cfg()
    browser = MagicMock()
    prov = APIProvider(cfg, browser)

    fake_lines = [
        b"data: " + json.dumps({
            "choices": [{"delta": {"content": "你好"}}]
        }).encode() + b"\n",
        b"data: [DONE]\n",
    ]
    mock_resp = _make_response(200, fake_lines)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.post = MagicMock(return_value=mock_resp)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
    with patch("ai_web2api.providers.api_provider.aiohttp.ClientSession", side_effect=FakeSession):
        chunks = [c async for c in prov.generate([{"role": "user", "content": "你好"}], "test-model")]

    content = "".join(c.text for c in chunks if c.kind == "content")
    assert content == "你好"


@pytest.mark.asyncio
async def test_generate_yields_thinking():
    """SSE 流正确解析 thinking。"""
    cfg = _cfg(api_thinking_path=["choices", "0", "delta", "reasoning_content"])
    browser = MagicMock()
    prov = APIProvider(cfg, browser)

    fake_lines = [
        b"data: " + json.dumps({
            "choices": [{"delta": {"reasoning_content": "思考中"}}]
        }).encode() + b"\n",
        b"data: " + json.dumps({
            "choices": [{"delta": {"content": "回答"}}]
        }).encode() + b"\n",
        b"data: [DONE]\n",
    ]
    mock_resp = _make_response(200, fake_lines)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.post = MagicMock(return_value=mock_resp)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
    with patch("ai_web2api.providers.api_provider.aiohttp.ClientSession", side_effect=FakeSession):
        chunks = [c async for c in prov.generate([{"role": "user", "content": "你好"}], "test-model")]

    thinking = "".join(c.text for c in chunks if c.kind == "thinking")
    content = "".join(c.text for c in chunks if c.kind == "content")
    assert thinking == "思考中"
    assert content == "回答"


@pytest.mark.asyncio
async def test_generate_api_error():
    """API 返回非 200 抛出 ProviderError。"""
    cfg = _cfg()
    browser = MagicMock()
    prov = APIProvider(cfg, browser)

    mock_resp = _make_response(401, [], text="Unauthorized")
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.post = MagicMock(return_value=mock_resp)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
    with patch("ai_web2api.providers.api_provider.aiohttp.ClientSession", side_effect=FakeSession):
        with pytest.raises(ProviderError, match="401"):
            async for _ in prov.generate([{"role": "user", "content": "hi"}], "test-model"):
                pass


@pytest.mark.asyncio
async def test_generate_missing_api_key():
    """api_key 未配置时直接抛 ProviderError。"""
    cfg = _cfg(api_key=None)
    browser = MagicMock()
    prov = APIProvider(cfg, browser)

    with pytest.raises(ProviderError, match="api_key 未配置"):
        async for _ in prov.generate([{"role": "user", "content": "hi"}], "test-model"):
            pass


@pytest.mark.asyncio
async def test_generate_retry_on_429():
    """429 触发重试（默认 3 次），最终成功。"""
    cfg = _cfg(api_retry_attempts=2, api_retry_backoff=0.01)
    browser = MagicMock()
    prov = APIProvider(cfg, browser)

    fake_lines = [
        b"data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]}).encode() + b"\n",
        b"data: [DONE]\n",
    ]

    err_resp = _make_response(429, [], text="Rate limit")
    ok_resp = _make_response(200, fake_lines)

    call_count = 0

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self._err_resp = err_resp
            self._ok_resp = ok_resp
        def post(self, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return self._err_resp
            return self._ok_resp
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False

    with patch("ai_web2api.providers.api_provider.aiohttp.ClientSession", side_effect=FakeSession):
        chunks = [c async for c in prov.generate([{"role": "user", "content": "hi"}], "test-model")]

    content = "".join(c.text for c in chunks if c.kind == "content")
    assert content == "ok"
    assert call_count == 2  # 第一次 429，第二次成功
