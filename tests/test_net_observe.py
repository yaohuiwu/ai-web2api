"""观测型旁听（network.observe）：把"卡住"归类为排队/长思考/流未结束。

运行：.venv/bin/python -m pytest tests/test_net_observe.py -v
"""

from __future__ import annotations

from ai_web2api.config import ProviderConfig
from ai_web2api.providers.webchat import WebChatProvider

URL = "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?bl=x"


class _Req:
    method = "POST"
    url = URL

    def __init__(self, timing: dict) -> None:
        self.timing = timing


class _Resp:
    url = URL

    def __init__(self, status: int, timing: dict) -> None:
        self.status = status
        self.request = _Req(timing)


class _StubBrowser:
    default_locale = "zh-CN"


def _prov(**network) -> WebChatProvider:
    cfg = ProviderConfig.model_validate(
        {
            "name": "gemini",
            "url": "https://gemini.google.com/app",
            "models": [{"name": "gemini-web"}],
            "network": network,
        }
    )
    return WebChatProvider(cfg, _StubBrowser())  # type: ignore[arg-type]


def test_observe_flag_requires_pattern():
    assert _prov().net_observe is False
    assert _prov(observe=True).net_observe is False          # 没 pattern 不算
    assert _prov(capture=False, observe=True, url_pattern="StreamGenerate").net_observe is True


def test_summary_stream_not_finished():
    """响应头已到但流没结束 → 说明站点"接受了请求但还在吐/卡住"，不是没发出。"""
    prov = _prov(capture=False, observe=True, url_pattern="StreamGenerate")
    prov._net_events = [_Resp(200, {"requestStart": 1000.0, "responseStart": 1800.0, "responseEnd": -1})]
    out = prov._net_summary()
    assert "status=200" in out and "响应头=0.8s" in out and "流未结束" in out


def test_summary_stream_finished():
    prov = _prov(capture=False, observe=True, url_pattern="StreamGenerate")
    prov._net_events = [_Resp(200, {"requestStart": 1000.0, "responseStart": 1100.0, "responseEnd": 5000.0})]
    out = prov._net_summary()
    assert "结束=4.0s" in out and "流已结束" in out


def test_summary_reports_limit_status():
    prov = _prov(capture=False, observe=True, url_pattern="StreamGenerate")
    prov._net_events = [_Resp(429, {"requestStart": 1000.0, "responseStart": 1050.0, "responseEnd": 1050.0})]
    assert "status=429" in prov._net_summary()


def test_summary_without_events():
    prov = _prov(capture=False, observe=True, url_pattern="StreamGenerate")
    assert "未捕获到匹配请求" in prov._net_summary()


def test_stop_observe_clears_events():
    prov = _prov(capture=False, observe=True, url_pattern="StreamGenerate")
    prov._net_events = [_Resp(200, {})]
    prov._net_handler = None
    prov._stop_observe(None)  # type: ignore[arg-type]
    assert prov._net_events == []
