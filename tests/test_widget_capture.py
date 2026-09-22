"""交互组件（iframe widget）：落盘 / id 校验 / 上限 / 清理 + provider 捕获。

运行：.venv/bin/python -m pytest tests/test_widget_capture.py -v
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ai_web2api.core.widgets import MAX_HTML_BYTES, WidgetStore


def test_save_and_read_back(tmp_path: Path):
    st = WidgetStore(tmp_path)
    meta = st.save("kimi", html=b"<html>hi</html>", png=b"\x89PNG-ish")
    assert meta and meta["html"] and meta["png"] and meta["bytes"] > 0
    assert st.path("kimi", meta["id"], "html").read_bytes() == b"<html>hi</html>"
    assert st.path("kimi", meta["id"], "png").read_bytes() == b"\x89PNG-ish"
    assert st.dir("kimi") == tmp_path / "kimi" / "widgets"


def test_path_rejects_bad_ids_and_exts(tmp_path: Path):
    st = WidgetStore(tmp_path)
    st.save("kimi", html=b"x")
    for bad in ("../etc/passwd", "a/b", "", None, "x" * 81, "has space"):
        assert st.path("kimi", bad, "html") is None, bad
    assert st.path("kimi", "w1-abc", "exe") is None          # 只允许 html/png
    assert st.path("kimi", "w1-abc", "html") is None          # 不存在


def test_caps_and_prune(tmp_path: Path):
    st = WidgetStore(tmp_path)
    assert st.save("kimi", html=b"x" * (MAX_HTML_BYTES + 1)) is None, "超限 HTML 应丢弃"
    assert st.save("kimi", html=b"", png=b"") is None
    # 超过 keep 个 → 删最旧
    for i in range(5):
        st.save("kimi", png=b"p" * 10)
        time.sleep(0.01)
    st.prune("kimi", keep=2)
    left = sorted(st.dir("kimi").glob("w*.*"))
    assert len(left) == 2


def test_html_and_png_independent(tmp_path: Path):
    st = WidgetStore(tmp_path)
    only_html = st.save("kimi", html=b"<b>h</b>")
    assert only_html["html"] and not only_html["png"]
    assert st.path("kimi", only_html["id"], "png") is None


# ---------- provider 捕获 ----------


class _Frame:
    def __init__(self, url, html="<html>w</html>", png=b"\x89PNG", fail=False):
        self.url = url
        self._html = html
        self._png = png
        self._fail = fail

    async def content(self):
        if self._fail:
            raise RuntimeError("cross-origin blocked")
        return self._html

    def locator(self, _sel):
        return self

    async def screenshot(self, **kw):
        if self._fail:
            raise RuntimeError("frame detached")
        return self._png


class _FramePage:
    def __init__(self, frames):
        self.frames = frames


def _prov(tmp_path: Path, **selectors):
    from ai_web2api.config import ProviderConfig
    from ai_web2api.providers.webchat import WebChatProvider

    class _Browser:
        profiles_dir = tmp_path

    cfg = ProviderConfig.model_validate(
        {
            "name": "kimi",
            "url": "https://www.kimi.com/",
            "models": [{"name": "kimi-web"}],
            "selectors": selectors,
        }
    )
    return WebChatProvider(cfg, _Browser())  # type: ignore[arg-type]


def test_capture_widgets_both(tmp_path: Path):
    prov = _prov(tmp_path, widget_capture="both")
    page = _FramePage([_Frame("https://old.kimi-canvas.com/"), _Frame("https://new.kimi-canvas.com/")])
    out = asyncio.run(prov._capture_widgets(page, {"https://old.kimi-canvas.com/"}))
    assert len(out) == 1 and out[0]["html"] and out[0]["png"]
    assert (tmp_path / "kimi" / "widgets" / f"{out[0]['id']}.html").is_file()


def test_capture_widgets_disabled_and_modes(tmp_path: Path):
    page = _FramePage([_Frame("https://n.kimi-canvas.com/")])
    assert asyncio.run(_prov(tmp_path).    _capture_widgets(page, set())) == []
    only_png = asyncio.run(_prov(tmp_path, widget_capture="png")._capture_widgets(page, set()))
    assert only_png and only_png[0]["png"] and not only_png[0]["html"]


def test_capture_tolerates_frame_errors(tmp_path: Path):
    """frame 读不到（跨域被拒/已销毁）→ 跳过，不抛。"""
    prov = _prov(tmp_path, widget_capture="both")
    page = _FramePage([_Frame("https://bad.kimi-canvas.com/", fail=True)])
    assert asyncio.run(prov._capture_widgets(page, set())) == []


def test_take_widgets_clears(tmp_path: Path):
    prov = _prov(tmp_path, widget_capture="html")
    prov._widgets = [{"id": "w1"}]
    assert prov.take_widgets() == [{"id": "w1"}]
    assert prov.take_widgets() == []


# ---------- 接口 / 历史 ----------


def _app(tmp_path: Path):
    import yaml
    from fastapi.testclient import TestClient

    from ai_web2api.main import create_app

    root = Path(__file__).resolve().parent.parent
    cfg = yaml.safe_load((root / "config.fake.yaml").read_text())
    cfg["profiles_dir"] = str(tmp_path)
    cfg.setdefault("server", {})["repo_url"] = None
    cfg["providers"]["fake"]["url"] = f"file://{root / 'tests' / 'fake_chat.html'}"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return TestClient(create_app(str(path)))


def test_widget_endpoints(tmp_path: Path):
    st = WidgetStore(tmp_path)
    meta = st.save("fake", html=b"<html>w</html>", png=b"\x89PNGdata")
    c = _app(tmp_path)
    png = c.get(f"/admin/fake/widgets/{meta['id']}.png")
    assert png.status_code == 200 and png.content == b"\x89PNGdata"
    html = c.get(f"/admin/fake/widgets/{meta['id']}.html")
    assert html.status_code == 200 and b"<html>w</html>" in html.content
    assert "sandbox allow-scripts" in html.headers["content-security-policy"], "必须加 CSP 沙箱"
    assert c.get("/admin/fake/widgets/nope.png").status_code == 404
    assert c.get("/admin/fake/widgets/..%2Fetc-passwd.html").status_code == 404


def test_messages_expose_widgets(tmp_path: Path):
    """历史消息要带上 widgets（Playground 回填组件面板）。"""
    from ai_web2api.config import ServerConfig
    from ai_web2api.core.threads import ThreadManager

    async def run():
        tm = ThreadManager(ServerConfig(thread_persist=True), tmp_path)
        await tm.save_turn("t1", "fake", "m", "问题", "回答", "思考", None, [{"id": "w1", "html": True}])
        return await tm.get_messages("t1")

    msgs = asyncio.run(run())
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["widgets"] == [{"id": "w1", "html": True}]
    assert msgs[0]["widgets"] is None


def test_capture_dedupes_mirrored_frames(tmp_path: Path):
    """同一组件常被站点镜像渲染多次（实测 4 帧 = 2 份）→ 按内容去重，只存一份。"""
    prov = _prov(tmp_path, widget_capture="both")
    same = dict(html="<html>same</html>", png=b"\x89PNGsame")
    page = _FramePage([
        _Frame("https://a.kimi-canvas.com/", **same),
        _Frame("https://b.kimi-canvas.com/", **same),          # 镜像副本
        _Frame("https://c.kimi-canvas.com/", html="<html>other</html>", png=b"\x89PNGother"),
    ])
    out = asyncio.run(prov._capture_widgets(page, set()))
    assert len(out) == 2, out
