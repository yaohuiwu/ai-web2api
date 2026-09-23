"""通用请求时间线：打点、派生指标、单行日志、内存缓冲。

运行：.venv/bin/python -m pytest tests/test_timeline.py -v
"""

from __future__ import annotations

import asyncio

import pytest

from ai_web2api.browser import extractor as ex
from ai_web2api.config import ProviderConfig
from ai_web2api.core.timeline import TIMELINES, RequestTimeline, TimelineLog
from ai_web2api.providers.webchat import WebChatProvider


class _StubBrowser:
    default_locale = "zh-CN"


class _NoLoc:
    @property
    def first(self):
        return self

    async def is_visible(self):
        return False


class _FakePage:
    async def wait_for_timeout(self, ms: float) -> None:
        await asyncio.sleep(ms / 1000)

    def locator(self, _sel):
        return _NoLoc()


def test_marks_and_derived_metrics(monkeypatch):
    tl = RequestTimeline(provider="doubao", model="doubao-web", thread="thread")
    clock = {"t": 100.0}
    monkeypatch.setattr("ai_web2api.core.timeline.time.monotonic", lambda: clock["t"])
    tl.started = 100.0

    clock["t"] = 103.9
    tl.mark("setup", overwrite=False)
    clock["t"] = 104.0
    tl.mark("send", overwrite=False)
    clock["t"] = 112.2
    tl.mark("first_content", overwrite=False)
    clock["t"] = 116.0
    tl.mark("settled")
    clock["t"] = 124.7
    tl.mark("settled")            # 覆盖 → 最终值 = 最后一次变化
    tl.mark("done")
    clock["t"] = 124.95
    tl.mark("final")

    assert tl.get("settled") == pytest.approx(24.7)
    assert tl.ttft == pytest.approx(8.2, abs=0.05)          # 首字延迟
    assert tl.settle_lag == pytest.approx(0.0, abs=0.01)      # settled 之后立刻 done
    assert tl.tail == pytest.approx(0.25, abs=0.01)
    line = tl.line()
    assert line.startswith("[timeline] provider=doubao")
    for key in ("setup=3.90", "send=4.00", "first_content=12.20", "done=24.70", "ttft=", "settle_lag="):
        assert key in line, line
    # key=value 可解析（后续入库依赖这个形状）
    fields = dict(p.split("=", 1) for p in line.replace("[timeline] ", "").split(" ") if "=" in p)
    assert fields["provider"] == "doubao" and float(fields["settled"]) == pytest.approx(24.7, abs=0.01)


def test_missing_marks_are_dash_and_derived_none():
    tl = RequestTimeline(provider="deepseek")
    tl.mark("send", overwrite=False)
    line = tl.line()
    assert "first_content=-" in line and "ttft=-" in line
    assert tl.ttft is None and tl.settle_lag is None


def test_first_content_mark_is_not_overwritten():
    tl = RequestTimeline(provider="x")
    tl.mark("first_content", overwrite=False)
    first = tl.get("first_content")
    tl.mark("first_content", overwrite=False)
    assert tl.get("first_content") == first


def test_timeline_log_ring_buffer():
    log = TimelineLog(maxlen=3)
    for i in range(5):
        tl = RequestTimeline(provider="doubao" if i % 2 else "chatgpt", thread=str(i))
        tl.mark("final")
        log.record(tl)
    assert len(log) == 3
    assert log.recent()[0]["thread"] == "4"                  # 新的在前
    assert [x["provider"] for x in log.recent(provider="doubao")] == ["doubao"]
    log.clear()
    assert len(log) == 0


@pytest.mark.asyncio
async def test_dom_poll_records_timeline_milestones(monkeypatch):
    """DOM 路径要打出 first_content / settled / done，并记录 polls 与 extract 耗时。"""
    cfg = ProviderConfig.model_validate({
        "name": "doubao", "url": "https://www.doubao.com/chat",
        "models": [{"name": "doubao-web"}],
        "selectors": {"response_container": [".md"], "thinking_container": []},
        "poll_interval": 0.01, "stable_polls": 2, "min_wait_before_stable": 0.0,
        "response_timeout": 5.0,
    })
    prov = WebChatProvider(cfg, _StubBrowser())  # type: ignore[arg-type]
    tl = RequestTimeline(provider="doubao", model="doubao-web")
    prov._tl = tl
    seq = ["第一段。", "第一段。第二段。"]
    state = {"n": 0}

    async def fake_count(_p, _sel):
        return 1

    async def fake_extract(_p, _sel, index=-1):
        state["n"] += 1
        return seq[min(state["n"] // 2, 1)]      # 两拍一变，随后稳定

    monkeypatch.setattr(ex, "count_matches", fake_count)
    monkeypatch.setattr(ex, "extract_markdown", fake_extract)

    chunks = [c async for c in prov._poll_response_dom(_FakePage(), {".md": 0}, {})]
    assert chunks, "应产出内容"
    assert tl.path == "dom"
    assert tl.get("first_content") is None, "首字由 generate() 消费 chunk 时打点（此处只测轮询）"
    assert tl.get("done") is not None
    assert tl.counts["polls"] > 0 and "extract_ms_total" in tl.counts
    assert tl.notes["finalize"] in {"stability", "stop_button"}
    assert tl.get("done") >= (tl.get("setup") or 0)


def test_admin_timeline_endpoint_lists_recent():
    """``/admin/timeline`` 能查到刚记录的时间线（内存缓冲，后续可换成入库）。"""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ai_web2api.api.routes import create_router
    from ai_web2api.config import AppConfig

    class _Registry:
        def __init__(self) -> None:
            self.config = AppConfig.model_validate(
                {"providers": [{"name": "deepseek", "url": "https://chat.deepseek.com",
                                "models": [{"name": "deepseek-web"}]}]}
            )

        def providers(self):
            return {}

        def list_models(self):
            return []

        def login_status(self):
            return {}

        def get_for_model(self, model):  # pragma: no cover - 本用例不生成
            raise AssertionError

    app = FastAPI()
    app.include_router(create_router(_Registry()))  # type: ignore[arg-type]
    tl = RequestTimeline(provider="deepseek", model="deepseek-web", thread="t1")
    tl.mark("send", overwrite=False)
    tl.mark("first_content", overwrite=False)
    tl.mark("settled")
    tl.mark("done")
    tl.mark("final")
    TIMELINES.record(tl)
    with TestClient(app) as client:
        data = client.get("/admin/timeline", params={"provider": "deepseek", "limit": 5}).json()
    assert data["count"] >= 1
    first = data["items"][0]
    assert first["provider"] == "deepseek" and "marks" in first and "settle_lag" in first
    assert first["ttft"] == pytest.approx(first["marks"]["first_content"] - first["marks"]["send"], abs=0.01)


@pytest.mark.asyncio
async def test_settled_is_marked_at_dom_change_not_at_emit(monkeypatch):
    """``settled`` 必须打"正文真正变化"的时刻，而不是"chunk 发出去"的时刻。

    背景：缓冲模式（stream_content: false）下 chunk 是**定稿之后**才发的，
    若按 chunk 打点会出现 settled > done（负的 settle_lag），指标就没意义了。
    """
    cfg = ProviderConfig.model_validate({
        "name": "chatgpt", "url": "https://chatgpt.com",
        "models": [{"name": "gpt-5-web"}],
        "selectors": {"response_container": [".md"], "thinking_container": [],
                      "stream_content": False},
        "poll_interval": 0.01, "stable_polls": 2, "min_wait_before_stable": 0.0,
        "response_timeout": 5.0,
    })
    prov = WebChatProvider(cfg, _StubBrowser())  # type: ignore[arg-type]
    tl = RequestTimeline(provider="chatgpt")
    prov._tl = tl
    state = {"n": 0}

    async def fake_count(_p, _sel):
        return 1

    async def fake_extract(_p, _sel, index=-1):
        state["n"] += 1
        return "完整正文" if state["n"] > 1 else "完"

    monkeypatch.setattr(ex, "count_matches", fake_count)
    monkeypatch.setattr(ex, "extract_markdown", fake_extract)

    chunks = [c async for c in prov._poll_response_dom(_FakePage(), {".md": 0}, {})]
    assert chunks, "缓冲模式定稿时应发一次完整正文"
    assert tl.get("settled") is not None, "应记录正文最后一次变化"
    assert tl.get("done") is not None
    assert tl.get("settled") <= tl.get("done"), "settled 不能晚于 done（否则 settle_lag 为负）"
    assert tl.settle_lag is not None and tl.settle_lag >= 0
