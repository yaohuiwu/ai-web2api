"""请求时间线（通用）：一次请求"慢在哪"的客观度量。

为什么要有它
------------
"感觉慢"很难排查：页面在写、我们没出字、还是站点在排队？所以给**所有 provider**
（网络通道 / DOM 通道、流式 / 非流式）统一打点，把每个关键时间点记下来：

    setup ── send ── first_think ── first_content ── settled ── done ── final
     页面就绪  已送达     思考首字        正文首字      正文不再变  我们判定结束  收尾

- ``ttft`` = first_content − send      → 用户感知的"首字延迟"
- ``settle_lag`` = done − settled      → **我们比站点慢多少**（"白等"，重点优化对象）
- ``tail`` = final − done              → 定稿补发/组件捕获开销
- ``total`` = final − started          → 本次请求总耗时

只用 ``time.monotonic()``（不受系统时钟调整影响），全部为**相对秒数**。

日志格式（单行 key=value，便于 grep/告警；后续可直接入库）::

    [timeline] provider=doubao model=doubao-web thread=thread path=dom finalize=stability \
      setup=3.90 send=4.02 first_think=- first_content=8.19 settled=16.06 done=24.70 \
      final=24.95 total=25.40 ttft=4.17 settle_lag=8.64 tail=0.25 polls=112 \
      extract_ms_avg=16 deltas=10 chars=349 think_chars=0 preview=1 stop_button=0
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# 打点顺序（用于输出与自检：后面的点不应早于前面的点）
ORDER = (
    "setup",
    "send",
    "first_think",
    "first_content",
    "settled",
    "done",
    "final",
)


@dataclass
class RequestTimeline:
    """单次请求的时间线（provider 层创建，路由/后台可读）。"""

    provider: str
    model: str = ""
    thread: str = "stateless"
    path: str = ""          # net / dom（数据通道）
    finalize: str = ""      # stability / stop_button / net_close / timeout_fallback
    started: float = field(default_factory=time.monotonic)
    marks: dict[str, float] = field(default_factory=dict)
    counts: dict[str, float] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    # ---------- 打点 ----------

    def mark(self, name: str, *, overwrite: bool = True) -> float:
        """记录一个时间点（相对 ``started`` 秒）。

        ``overwrite=True``（默认）时**重复打点会覆盖**——例如 ``settled`` 每次正文变化都打，
        最终值就是"正文最后一次变化"的时刻。``overwrite=False`` 只记第一次（如 ``first_content``）。
        """
        if not overwrite and name in self.marks:
            return self.marks[name]
        value = time.monotonic() - self.started
        self.marks[name] = value
        return value

    def bump(self, name: str, value: float = 1.0) -> None:
        self.counts[name] = self.counts.get(name, 0.0) + value

    def note(self, key: str, value: Any) -> None:
        self.notes[key] = str(value)

    def get(self, name: str) -> float | None:
        return self.marks.get(name)

    # ---------- 派生指标 ----------

    def rel(self, a: str, b: str) -> float | None:
        """``b - a``（任一缺失 → ``None``）。"""
        va, vb = self.marks.get(a), self.marks.get(b)
        return None if va is None or vb is None else round(vb - va, 2)

    @property
    def ttft(self) -> float | None:
        """首个正文增量相对"已送达"的延迟（用户感知首字）。"""
        return self.rel("send", "first_content")

    @property
    def settle_lag(self) -> float | None:
        """正文不再变化 → 我们判定结束之间的间隔（"白等"）。"""
        return self.rel("settled", "done")

    @property
    def tail(self) -> float | None:
        return self.rel("done", "final")

    def as_dict(self) -> dict[str, Any]:
        """结构化输出（内存缓冲/未来入库用）。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "thread": self.thread,
            "path": self.path,
            "finalize": self.finalize or self.notes.get("finalize", ""),
            "ts": round(time.time()),
            "marks": {k: round(v, 3) for k, v in self.marks.items()},
            "counts": {k: round(v, 3) for k, v in self.counts.items()},
            "notes": dict(self.notes),
            "ttft": self.ttft,
            "settle_lag": self.settle_lag,
            "tail": self.tail,
            "total": round(self.marks.get("final", 0.0), 3) or None,
        }

    def line(self) -> str:
        """单行日志：``[timeline] key=value …``（缺失的时间点记 ``-``）。"""

        def fmt(name: str) -> str:
            v = self.marks.get(name)
            return "-" if v is None else f"{v:.2f}"

        parts = [
            f"provider={self.provider}",
            f"model={self.model}" if self.model else "",
            f"thread={self.thread}",
            f"path={self.path}" if self.path else "",
            f"finalize={self.finalize}" if self.finalize else "",
            *[f"{name}={fmt(name)}" for name in ORDER],
        ]
        for key in ("ttft", "settle_lag", "tail"):
            v = getattr(self, key)
            parts.append(f"{key}={'-' if v is None else f'{v:.2f}'}")
        for key in sorted(self.counts):
            v = self.counts[key]
            parts.append(f"{key}={v:g}")
        for key in sorted(self.notes):
            parts.append(f"{key}={self.notes[key]}")
        return "[timeline] " + " ".join(p for p in parts if p)

    def log(self) -> None:
        logger.info(self.line())


class TimelineLog:
    """最近 N 条时间线（内存环形缓冲）。

    "先记日志、后面考虑入库"：这里先给 ``/admin/timeline`` 一个可查的窗口；
    真要长期留存时，把它换成写库/写文件即可（``RequestTimeline.as_dict()`` 已是纯数据）。
    """

    def __init__(self, maxlen: int = 200) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def record(self, tl: RequestTimeline) -> None:
        self._items.append(tl.as_dict())

    def recent(self, provider: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        items = list(self._items)
        if provider:
            items = [x for x in items if x.get("provider") == provider]
        return items[-limit:][::-1]      # 新的在前

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


TIMELINES = TimelineLog()
