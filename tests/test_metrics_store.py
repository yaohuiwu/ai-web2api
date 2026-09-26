"""Metrics（provider 请求指标）单元测试。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from ai_web2api.core.store import ThreadStore


def _store(tmp_path: Path) -> ThreadStore:
    return ThreadStore(tmp_path / "threads.db")


def test_record_and_aggregate(tmp_path: Path):
    st = _store(tmp_path)
    now = time.time()
    # 模拟 3 次请求：2 成功 1 失败
    st.record_metric("doubao", ok=True, total=10, ttft=3, finalize="stability")
    st.record_metric("doubao", ok=True, total=20, ttft=4, finalize="timeout_fallback")
    st.record_metric("doubao", ok=False, total=30, ttft=5, finalize="exception", error="timeout")
    agg = st.aggregate_metrics(window_hours=99999)
    # 去掉末尾 ranking 尾元素
    vals = [x for x in agg if x.get("name")]
    assert len(vals) == 1
    d = vals[0]
    assert d["name"] == "doubao"
    assert d["n"] == 3
    assert d["ok_n"] == 2
    assert d["success_rate"] == pytest.approx(66.7, abs=0.1)
    assert d["avg_total"] == pytest.approx(20.0, abs=0.01)
    assert d["p50_total"] == 20.0
    # p95 线性插值: sorted=[10,20,30], k=(3-1)*0.95=1.9 → 20+10*0.9=29.0
    assert d["p95_total"] == pytest.approx(29.0, abs=0.01)
    assert d["avg_ttft"] == pytest.approx(4.0, abs=0.01)
    assert d["finalize_counts"] == {"stability": 1, "timeout_fallback": 1, "exception": 1}
    assert d["last_ok"] is False
    assert d["last_error"] == "timeout"


def test_ranking(tmp_path: Path):
    st = _store(tmp_path)
    st.record_metric("a", ok=True, total=5)
    st.record_metric("a", ok=True, total=5)
    st.record_metric("b", ok=True, total=50)
    st.record_metric("b", ok=False, total=50)
    agg = [x for x in st.aggregate_metrics(window_hours=99999) if x.get("name")]
    assert [x["name"] for x in agg] == ["a", "b"]  # a 100% > b 50%


def test_empty_aggregate(tmp_path: Path):
    st = _store(tmp_path)
    agg = st.aggregate_metrics(window_hours=1)
    assert agg == []


def test_clear_old_metrics(tmp_path: Path):
    st = _store(tmp_path)
    st.record_metric("doubao", ok=True, total=1)
    assert st.clear_metrics_older_than(days=0) >= 1  # 0 天前 = 全部删除
    agg = st.aggregate_metrics(window_hours=99999)
    assert not any(x.get("name") for x in agg)


def test_window_filter(tmp_path: Path):
    st = _store(tmp_path)
    st.record_metric("doubao", ok=True, total=1)
    # 窗口 1h 应该过滤掉（记录时间是 now, 但窗口过去的时间戳被排除在聚合外）
    # 实际：record_metric 用 DB 默认 strftime('%s','now') 写入，是当前时间 → 在 1h 窗口内
    agg = st.aggregate_metrics(window_hours=1)
    vals = [x for x in agg if x.get("name")]
    assert len(vals) == 1 and vals[0]["n"] == 1
    # 24h 窗口也一样
    agg24 = st.aggregate_metrics(window_hours=24)
    vals24 = [x for x in agg24 if x.get("name")]
    assert len(vals24) == 1 and vals24[0]["n"] == 1
