"""画面交互（control）：参数校验、坐标夹取、拖拽插值、真实输入调用序列、403 开关。

设计见 docs/LIVE_CONTROL.md。默认关闭（server.live_control=false → 403）。

运行：.venv/bin/python -m pytest tests/test_live_control.py -v
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_web2api.browser.control import ACTIONS, InputAction, apply_input, clamp_point, interpolate


class _Mouse:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def move(self, x, y):
        self.calls.append(("move", round(x), round(y)))

    async def down(self, **kw):
        self.calls.append(("down", kw.get("button", "left")))

    async def up(self, **kw):
        self.calls.append(("up", kw.get("button", "left")))

    async def click(self, x, y, **kw):
        self.calls.append(("click", round(x), round(y)))

    async def wheel(self, dx, dy):
        self.calls.append(("wheel", dx, dy))


class _Keyboard:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def type(self, text):
        self.calls.append(("type", text))

    async def press(self, key):
        self.calls.append(("press", key))


class _Page:
    def __init__(self, fail_on=None) -> None:
        self.mouse = _Mouse()
        self.keyboard = _Keyboard()
        self.viewport_size = {"width": 1440, "height": 900}
        self.reloaded = 0
        self.goto_calls: list[str] = []
        self.url = "https://example.com/c/abc"
        self._fail_on = fail_on

    def is_closed(self) -> bool:
        return False

    async def reload(self, **_kw):
        self.reloaded += 1

    async def goto(self, url, **_kw):
        self.goto_calls.append(url)
        self.url = url


def test_validate_rejects_bad_input():
    assert InputAction(action="nope").validate()
    assert InputAction(action="click").validate(), "click 必须有坐标"
    assert InputAction(action="drag", x=1, y=1).validate(), "drag 必须有终点"
    assert InputAction(action="key").validate()
    assert InputAction(action="type", text="x" * 5000).validate()
    for a in ACTIONS:
        assert InputAction(action=a, x=1, y=1, x2=2, y2=2, text="hi", key="Enter").validate() is None


def test_clamp_and_interpolate():
    assert clamp_point(-5, 99999, {"width": 100, "height": 200}) == (0.0, 199.0)
    path = interpolate(0, 0, 100, 50, 4)
    assert path[-1] == (100.0, 50.0) and len(path) == 4
    assert interpolate(0, 0, 10, 0, 1000)[:2] == [(10 / 60, 0.0), (20 / 60, 0.0)]   # 步数有上限


@pytest.mark.asyncio
async def test_click_is_move_then_click():
    """先 hover 再点：很多控件的按钮/菜单是 hover 才出现。"""
    page = _Page()
    out = await apply_input(page, InputAction(action="click", x=10, y=20))
    assert page.mouse.calls == [("move", 10, 20), ("click", 10, 20)]
    assert out["mapped"] == {"x": 10, "y": 20}


@pytest.mark.asyncio
async def test_drag_always_releases_even_on_failure():
    """拖拽必须 down→移动→up；即使中途异常也要补发 up（否则站点卡在拖拽态）。"""
    page = _Page()
    out = await apply_input(page, InputAction(action="drag", x=5, y=5, x2=60, y2=40, steps=4, delay_ms=5))
    kinds = [c[0] for c in page.mouse.calls]
    assert kinds[0] == "move" and kinds[1] == "down" and kinds[-1] == "up"
    assert out["steps"] == 4 and out["mapped"]["x2"] == 60

    class _Boom(_Page):
        async def _noop(self):
            return None

    boom = _Boom()
    orig = boom.mouse.move

    async def flaky(x, y):
        await orig(x, y)
        if len(boom.mouse.calls) > 3:
            raise RuntimeError("页面在动")

    boom.mouse.move = flaky  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        await apply_input(boom, InputAction(action="drag", x=1, y=1, x2=50, y2=50, steps=4, delay_ms=5))
    assert boom.mouse.calls[-1][0] == "up", "异常路径也要松开"


@pytest.mark.asyncio
async def test_type_key_wheel_reload_to_bottom():
    page = _Page()
    await apply_input(page, InputAction(action="type", text="你好"))
    await apply_input(page, InputAction(action="key", key="Enter"))
    await apply_input(page, InputAction(action="wheel", x=1, y=1, dy=300))
    await apply_input(page, InputAction(action="reload"))
    await apply_input(page, InputAction(action="to_bottom"))
    assert ("type", "你好") in page.keyboard.calls
    assert ("press", "Enter") in page.keyboard.calls
    assert ("wheel", 0, 300) in page.mouse.calls
    assert page.reloaded == 1
    assert ("wheel", 0, 10000) in page.mouse.calls


@pytest.mark.asyncio
async def test_reopen_navigates_to_home_not_reload():
    """reopen = 导航回 provider 首页（人机验证/页面卡住时用），与 reload（原地刷新）区分。"""
    page = _Page()
    out = await apply_input(page, InputAction(action="reopen"), reopen_url="https://example.com/")
    assert page.goto_calls == ["https://example.com/"]
    assert page.reloaded == 0, "reopen 不应退化成 reload"
    assert out["url"] == "https://example.com/"


@pytest.mark.asyncio
async def test_reopen_without_reopen_url_falls_back_to_reload():
    """未提供 reopen_url（旧调用方/测试）→ 退化为刷新当前页，不报错。"""
    page = _Page()
    out = await apply_input(page, InputAction(action="reopen"))
    assert page.reloaded == 1 and out.get("reloaded") is True


def _client(control: bool) -> TestClient:
    from types import SimpleNamespace

    from ai_web2api.api.routes import create_router
    from ai_web2api.config import AppConfig

    class _Registry:
        def __init__(self) -> None:
            self.config = AppConfig.model_validate(
                {
                    "server": {"live_control": control},
                    "providers": [{"name": "fake", "url": "https://x", "models": [{"name": "fake-web"}]}],
                }
            )

        def providers(self):
            return {}

        def list_models(self):
            return []

        def login_status(self):
            return {}

        def get_for_model(self, m):  # pragma: no cover
            raise AssertionError

    app = FastAPI()
    app.include_router(create_router(_Registry()))  # type: ignore[arg-type]
    return TestClient(app)


def test_input_disabled_returns_403():
    """默认关闭：没开 live_control 时一律 403（UI 也会隐藏交互区）。"""
    r = _client(control=False).post("/admin/fake/input", json={"action": "click", "x": 1, "y": 1})
    assert r.status_code == 403 and "live_control" in r.text


def test_input_rejects_unknown_action_and_missing_coords():
    c = _client(control=True)
    assert c.post("/admin/fake/input", json={"action": "nope"}).status_code == 400
    assert c.post("/admin/fake/input", json={"action": "click"}).status_code == 400
    # 已知 provider 校验：未知 provider → 404（先过参数校验）
    assert c.post("/admin/nope/input", json={"action": "click", "x": 1, "y": 1}).status_code == 404


def _reopen_client(page: _Page, *, bound_thread_id=None, url_id=None):
    """带假 provider/threads 的路由：验证 reopen 的目标选择（会话页 vs 首页）。"""
    from types import SimpleNamespace

    from ai_web2api.api.routes import create_router
    from ai_web2api.config import AppConfig

    class _Provider:
        def __init__(self) -> None:
            self.cfg = SimpleNamespace(url="https://chat.example.com/")

        def session_url(self, sid: str) -> str:
            return f"https://chat.example.com/c/{sid}"

    class _Threads:
        def __init__(self) -> None:
            self.session = SimpleNamespace(lock=None, url_id=url_id)

        def live_pages(self, _name):
            return [(bound_thread_id, page)] if bound_thread_id else []

        def get(self, _tid):
            return self.session

    class _Ctx:
        pages = [page]

    class _Registry:
        def __init__(self) -> None:
            self.config = AppConfig.model_validate(
                {
                    "server": {"live_control": True},
                    "providers": [{"name": "fake", "url": "https://chat.example.com/", "models": []}],
                }
            )
            self.browser = SimpleNamespace(active_context=lambda _n: _Ctx())

        def providers(self):
            return {"fake": _Provider()}

        def login_status(self):
            return {}

    app = FastAPI()
    app.include_router(create_router(_Registry(), _Threads()))  # type: ignore[arg-type]
    return TestClient(app)


def test_reopen_route_goes_to_session_url_when_thread_bound():
    """会话页 reopen → 重开该会话 URL（不丢上下文）。"""
    page = _Page()
    c = _reopen_client(page, bound_thread_id="t1", url_id="abc123")
    r = c.post("/admin/fake/input", json={"action": "reopen"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert page.goto_calls == ["https://chat.example.com/c/abc123"]
    assert page.reloaded == 0


def test_reopen_route_goes_home_when_no_session():
    """非会话页（无活跃 thread）reopen → provider 首页。"""
    page = _Page()
    c = _reopen_client(page)
    r = c.post("/admin/fake/input", json={"action": "reopen"})
    assert r.status_code == 200
    assert page.goto_calls == ["https://chat.example.com/"]
