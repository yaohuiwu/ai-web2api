"""交互组件（iframe widget）落盘与读取。

组件的 HTML 是"**从渲染结果里抓的**"（含内联脚本），因此：
- Playground 必须以 ``sandbox="allow-scripts"`` 呈现（不给 ``allow-same-origin``）；
- 文件接口只允许服务端生成的 id（正则白名单，防路径穿越）。
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_HTML_BYTES = 512 * 1024        # 单组件 HTML 上限（实测 28KB）
MAX_PNG_BYTES = 2 * 1024 * 1024    # 单组件截图上限（实测 56–60KB）
KEEP_PER_PROVIDER = 200            # 每 provider 最多保留的文件数（按 mtime 新→旧）
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class WidgetStore:
    """``profiles/<provider>/widgets/`` 下的组件文件（html / png）。"""

    def __init__(self, profiles_dir: str | Path) -> None:
        self._root = Path(profiles_dir)

    def dir(self, provider: str) -> Path:
        return self._root / provider / "widgets"

    def save(self, provider: str, *, html: bytes | None = None, png: bytes | None = None) -> dict | None:
        """写入一个组件；返回元数据 ``{id, html, png, bytes}``（超限/空则返回 None）。"""
        if html and len(html) > MAX_HTML_BYTES:
            logger.info("组件 HTML 超限（%d > %d），丢弃", len(html), MAX_HTML_BYTES)
            html = None
        if png and len(png) > MAX_PNG_BYTES:
            logger.info("组件截图超限（%d > %d），丢弃", len(png), MAX_PNG_BYTES)
            png = None
        if not html and not png:
            return None
        widget_id = f"w{int(time.time() * 1000)}-{secrets.token_hex(3)}"
        directory = self.dir(provider)
        directory.mkdir(parents=True, exist_ok=True)
        meta: dict = {"id": widget_id, "html": False, "png": False, "bytes": 0}
        if html:
            (directory / f"{widget_id}.html").write_bytes(html)
            meta["html"], meta["bytes"] = True, meta["bytes"] + len(html)
        if png:
            (directory / f"{widget_id}.png").write_bytes(png)
            meta["png"], meta["bytes"] = True, meta["bytes"] + len(png)
        self.prune(provider)
        logger.info("组件已保存 %s/%s（html=%s png=%s %d bytes）", provider, widget_id, meta["html"], meta["png"], meta["bytes"])
        return meta

    def path(self, provider: str, widget_id: str | None, ext: str) -> Path | None:
        """校验并返回文件路径（id 白名单；不存在返回 None）。"""
        if ext not in ("html", "png") or not _ID_RE.match(widget_id or ""):
            return None
        path = self.dir(provider) / f"{widget_id}.{ext}"
        return path if path.is_file() else None

    def prune(self, provider: str, keep: int = KEEP_PER_PROVIDER) -> int:
        """按 mtime 保留最新的 ``keep`` 个文件，删多余的。返回删除数。"""
        try:
            files = sorted(
                (p for p in self.dir(provider).glob("w*.*") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return 0
        removed = 0
        for path in files[keep:]:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed
