"""通用 API provider：直接调用 HTTP API（无需浏览器）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, AsyncIterator

import aiohttp

from ..core.timeline import RequestTimeline
from ..core.errors import ProviderError
from .base import BaseProvider, StreamChunk

logger = logging.getLogger(__name__)

_ENV_RE = re.compile(r"^\$\{(\w+)\}$")


def resolve_api_key(raw: str | None) -> str | None:
    """解析 API Key：支持 ${ENV_VAR} 语法，未设置返回 None。"""
    if not raw:
        return None
    m = _ENV_RE.match(raw.strip())
    if m:
        val = os.environ.get(m.group(1), "")
        return val or None
    return raw


def _json_path(obj: Any, path: list[str]) -> str | None:
    """按 JSON 路径提取值，缺失返回 None。"""
    cur: Any = obj
    for k in path:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        elif isinstance(cur, list):
            try:
                cur = cur[int(k)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    if cur is None:
        return None
    return str(cur) if not isinstance(cur, str) else cur


async def _async_iter(items: list[bytes]):
    """将字节列表转为异步迭代器。"""
    for item in items:
        yield item


class APIProvider(BaseProvider):
    """通用 HTTP API provider（OpenAI 兼容 / 智谱 / 通义 等）。

    复用 ``BaseProvider.generate()`` 签名，复用指标系统（``metrics_store``）。
    不需要浏览器、不需要登录态。支持 Function Calling / Tools。
    """

    async def check_login(self) -> bool:
        """API provider 无需登录，始终返回 True。"""
        return True

    async def generate(
        self,
        messages: list[dict],
        model: str,
        *,
        thread_mode: str | None = None,
        thread_page=None,
        mode: str | None = None,
        deep_think: bool | None = None,
        search: bool | None = None,
        attachments: list[dict] | None = None,
        options: dict | None = None,
        prompt_override: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        cfg = self.cfg
        api_key = resolve_api_key(cfg.api_key)
        if not api_key:
            raise ProviderError(
                f"provider {self.name} 的 api_key 未配置（用 ${{ENV_VAR}} 语法或直接写 Key）",
                provider=self.name,
            )

        base_url = cfg.api_base or cfg.url.rstrip("/")
        model = model or cfg.api_model or self.exposed_models[0]
        attempts = max(0, cfg.api_retry_attempts) + 1  # 总尝试次数（含首次）

        tl = RequestTimeline(provider=self.name, model=model, thread=thread_mode or "stateless")
        self._tl = tl
        got_content = False

        for attempt in range(1, attempts + 1):
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": True,
            }
            # Function Calling 支持：options 透传 tools
            if options:
                payload.update(options)

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                **cfg.api_headers,
            }

            timeout = aiohttp.ClientTimeout(total=cfg.api_timeout)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        base_url,
                        headers=headers,
                        json=payload,
                    ) as resp:
                        if resp.status != 200:
                            text = await resp.text()
                            # 429/502/503/504 视为可重试错误
                            if resp.status in (429, 502, 503, 504):
                                raise ProviderError(
                                    f"API {resp.status}: {text[:300]}",
                                    provider=self.name,
                                    retryable=True,
                                )
                            raise ProviderError(
                                f"API {resp.status}: {text[:300]}",
                                provider=self.name,
                            )

                        async for raw_line in resp.content:
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            if not line:
                                continue
                            prefix = cfg.api_stream_prefix
                            if not line.startswith(prefix):
                                continue
                            data = line[len(prefix):].strip()
                            if data == cfg.api_done_marker:
                                tl.mark("final")
                                tl.ok = got_content
                                tl.error = ""
                                break
                            try:
                                obj = json.loads(data)
                            except json.JSONDecodeError:
                                continue

                            content = _json_path(obj, cfg.api_content_path)
                            if content:
                                got_content = True
                                yield StreamChunk(kind="content", text=content)

                            if cfg.api_thinking_path:
                                thinking = _json_path(obj, cfg.api_thinking_path)
                                if thinking:
                                    yield StreamChunk(kind="thinking", text=thinking)

                        # 结束后记录指标
                        store = getattr(self, "metrics_store", None)
                        if store is not None:
                            try:
                                loop = asyncio.get_running_loop()
                                loop.run_in_executor(
                                    None,
                                    store.record_metric,
                                    self.name, tl.ok,
                                    tl.marks.get("final"),
                                    tl.ttft, tl.settle_lag, tl.tail,
                                    tl.finalize or "", tl.error or "",
                                    model,
                                    thread_mode or "stateless",
                                )
                            except Exception:  # noqa: BLE001
                                logger.debug("metrics record failed", exc_info=True)
                        break  # 成功，退出重试循环
            except ProviderError as exc:
                # 可重试错误（429/502/503/504）
                retryable = exc.retryable or isinstance(exc, asyncio.TimeoutError)
                if attempt < attempts and retryable:
                    backoff = cfg.api_retry_backoff * (2 ** (attempt - 1))
                    logger.warning(
                        "[%s] attempt %d/%d 失败（%s），%ss 后重试",
                        self.name, attempt, attempts, exc, backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                # 不可重试或重试耗尽
                tl.mark("final")
                tl.ok = False
                tl.error = str(exc)
                raise
            except asyncio.TimeoutError as exc:
                tl.mark("final")
                tl.ok = False
                tl.error = str(exc)
                raise
            except Exception as exc:  # noqa: BLE001
                tl.mark("final")
                tl.ok = False
                tl.error = str(exc)
                raise
        else:
            # 重试耗尽
            tl.mark("final")
            tl.ok = False
            tl.error = str(last_exc) if (last_exc := None) else "retry exhausted"

        self._tl = None
