"""画面交互（control）：把 UI 的点击/拖拽/输入翻译成 Playwright **真实事件**。

设计见 docs/LIVE_CONTROL.md。要点：
- **默认关闭**：只有 ``server.live_control=true`` 时路由才接受 /input（否则 403）；
- 事件是真实输入（``isTrusted=true``）→ 站点组件/验证码都认；
- ``reopen`` 只去调用方指定的同源目标（会话页或 provider 首页），不做任意 URL 跳转；
- 拖拽在**一次请求内**完成（起点→down→插值移动→up），并在 ``finally`` 里补发 ``up``，
  避免"漏发 up"导致站点卡在拖拽态、后续点击全乱；
- 坐标是 **CSS 像素**（与 ``page.mouse`` 一致），越界夹取，不报错。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page

# 允许的动作（少而必要；不做任意 JS 执行）
# `reopen` = 重开页面（会话页→该会话 URL；否则 provider 首页），与 `reload`（原地刷新）区分；
# 只能去调用方传入的 reopen_url（同源白名单在路由层保证），不接受任意 URL。
ACTIONS = ("click", "move", "down", "up", "drag", "wheel", "type", "key", "reload", "reopen", "to_bottom")

MAX_STEPS = 60
MIN_STEP_DELAY_MS = 5
MAX_TEXT_LEN = 4000


@dataclass
class InputAction:
    """一次输入请求（前端 → 后端）。"""

    action: str
    x: float | None = None
    y: float | None = None
    x2: float | None = None
    y2: float | None = None
    dx: float = 0.0
    dy: float = 0.0
    text: str = ""
    key: str = ""
    steps: int = 12
    delay_ms: int = 25
    button: str = "left"
    extra: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> str | None:
        """参数校验；返回错误信息（None = 通过）。"""
        if self.action not in ACTIONS:
            return f"未知 action：{self.action}（可用：{', '.join(ACTIONS)}）"
        need_xy = {"click", "move", "down", "up", "drag"}
        if self.action in need_xy and (self.x is None or self.y is None):
            return f"{self.action} 需要 x/y"
        if self.action == "drag" and (self.x2 is None or self.y2 is None):
            return "drag 需要 x2/y2（终点）"
        if self.action == "type" and len(self.text) > MAX_TEXT_LEN:
            return f"type 文本过长（>{MAX_TEXT_LEN}）"
        if self.action == "key" and not self.key:
            return "key 需要 key（如 Enter/Escape/Tab/ArrowDown/Backspace）"
        return None


def clamp_point(x: float, y: float, viewport: dict | None) -> tuple[float, float]:
    """把坐标夹到视口内（越界按边缘处理，不报错）。"""
    vw = float((viewport or {}).get("width") or 1440)
    vh = float((viewport or {}).get("height") or 900)
    return (min(max(0.0, float(x)), vw - 1), min(max(0.0, float(y)), vh - 1))


def interpolate(x1: float, y1: float, x2: float, y2: float, steps: int) -> list[tuple[float, float]]:
    """拖拽路径插值（含终点）。真实拖拽需要中间 move，一步到位多数组件不认。"""
    n = max(2, min(int(steps or 12), MAX_STEPS))
    return [
        (x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n)
        for i in range(1, n + 1)
    ]


async def apply_input(
    page: Page, req: InputAction, *, reopen_url: str | None = None
) -> dict[str, Any]:
    """执行一次输入动作；返回摘要（含实际坐标/步数），供 UI 与日志使用。

    ``reopen_url``：``reopen`` 的目标（会话页 URL 或 provider 首页，由路由层决定）。
    未提供时 ``reopen`` 退化为原地 ``reload``（不报错，保证旧调用方/测试仍可用）。
    """
    t0 = time.monotonic()
    vp = page.viewport_size or {}
    out: dict[str, Any] = {"action": req.action, "viewport": vp}

    if req.action == "reopen":
        if not reopen_url:
            await page.reload(wait_until="domcontentloaded", timeout=30_000)
            return {**out, "reloaded": True, "elapsed_ms": round((time.monotonic() - t0) * 1000)}
        await page.goto(reopen_url, wait_until="domcontentloaded", timeout=30_000)
        return {
            **out,
            "url": page.url,
            "elapsed_ms": round((time.monotonic() - t0) * 1000),
        }

    if req.action == "reload":
        await page.reload(wait_until="domcontentloaded", timeout=30_000)
        return {**out, "elapsed_ms": round((time.monotonic() - t0) * 1000)}

    if req.action == "to_bottom":
        # 用滚轮滚到底（不动键盘焦点，避免干扰页面上的输入框）
        await page.mouse.move(*clamp_point(720, 450, vp))
        await page.mouse.wheel(0, 10000)
        return {**out, "elapsed_ms": round((time.monotonic() - t0) * 1000)}

    if req.action == "type":
        await page.keyboard.type(req.text)
        return {**out, "chars": len(req.text), "elapsed_ms": round((time.monotonic() - t0) * 1000)}

    if req.action == "key":
        await page.keyboard.press(req.key)
        return {**out, "key": req.key, "elapsed_ms": round((time.monotonic() - t0) * 1000)}

    x, y = clamp_point(req.x or 0, req.y or 0, vp)
    out["mapped"] = {"x": round(x), "y": round(y)}

    if req.action == "wheel":
        await page.mouse.move(x, y)
        await page.mouse.wheel(req.dx, req.dy)
        return {**out, "dx": req.dx, "dy": req.dy, "elapsed_ms": round((time.monotonic() - t0) * 1000)}

    if req.action == "move":
        await page.mouse.move(x, y)
    elif req.action == "down":
        await page.mouse.move(x, y)
        await page.mouse.down(button=req.button)
    elif req.action == "up":
        await page.mouse.move(x, y)
        await page.mouse.up(button=req.button)
    elif req.action == "click":
        await page.mouse.move(x, y)          # 先 hover：很多控件的工具栏/菜单是 hover 才出现
        await page.mouse.click(x, y, button=req.button)
    elif req.action == "drag":
        x2, y2 = clamp_point(req.x2 or 0, req.y2 or 0, vp)
        out["mapped"]["x2"] = round(x2)
        out["mapped"]["y2"] = round(y2)
        path = interpolate(x, y, x2, y2, req.steps)
        delay = max(MIN_STEP_DELAY_MS, int(req.delay_ms or 25)) / 1000
        await page.mouse.move(x, y)
        await page.mouse.down(button=req.button)
        try:
            for px, py in path:
                await page.mouse.move(px, py)
                await asyncio.sleep(delay)
        finally:
            # ⚠️ 无论如何都要松开：漏发 up 会让站点卡在拖拽态（后续点击全乱）
            await page.mouse.up(button=req.button)
        out["steps"] = len(path)

    out["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
    return out
