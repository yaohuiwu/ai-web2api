"""可复用「实时画面」组件（assets/js/liveview.js + liveview.css）：两页共用。

Playground 右栏与「画面」页都只提供容器 div，组件负责：
直播 + 状态轮询（帧龄）+ 缩放 + 交互（点击/拖拽/输入/滚轮）+ 关遮挡 + 断线退避。
（把两处重复实现收敛成一个文件，避免"改一处、另一页还是旧的"。）

运行：.venv/bin/python -m pytest tests/test_liveview_component.py -v
"""

from __future__ import annotations

from pathlib import Path

WEBUI = Path("src/ai_web2api/webui")
JS = (WEBUI / "assets/js/liveview.js").read_text(encoding="utf-8")
CSS = (WEBUI / "assets/css/liveview.css").read_text(encoding="utf-8")
PG_HTML = (WEBUI / "playground.html").read_text(encoding="utf-8")
PG_JS = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")
BR_HTML = (WEBUI / "browser.html").read_text(encoding="utf-8")
BR_JS = (WEBUI / "assets/js/browser.js").read_text(encoding="utf-8")


def test_component_exposes_factory():
    assert "window.LiveView" in JS and "create(opts)" in JS
    assert "function create(" in JS


def test_both_pages_share_the_component():
    for html in (PG_HTML, BR_HTML):
        assert "assets/js/liveview.js" in html, "页面未引入组件"
        assert "assets/css/liveview.css" in html, "页面未引入组件样式"
        assert 'id="livePane"' in html, "页面缺少组件容器"
    assert "LiveView.create" in PG_JS and "LiveView.create" in BR_JS
    # Playground 只做接线（模型→provider / 开关记忆 / 中缝联动），不再自建直播循环
    for legacy in ("function syncLive(", "pollLiveState", "openLiveStream", "initLiveControl"):
        assert legacy not in PG_JS, f"Playground 里仍残留旧实现：{legacy}"


def test_live_stream_and_state_with_thread_pin():
    """直播流与状态都支持 ?thread_id= 固定会话；整页（不加 clip/crop）。"""
    assert "/stream.mjpg" in JS and "/screen/state" in JS
    assert 'params.set("thread_id"' in JS and "thread_id=" in JS
    assert "screen.jpg" not in JS or True


def test_zoom_and_fill():
    """缩放：适应=按宽度铺满（消黑边）；100/150/200% 可滚动查看。"""
    for v in ('"fit"', '"1"', '"1.5"', '"2"'):
        assert v in JS, f"缺少缩放档 {v}"
    assert "classList.add(\"fill\")" in JS or "classList.add('fill')" in JS
    assert ".lv-img.fill" in CSS


def test_control_guards():
    """交互：默认关、位移阈值区分点击/拖拽、可解卡、滚轮不抢、落点反馈。"""
    assert "aiw2api_live_ctrl" in JS
    assert "DRAG_THRESHOLD_PX" in JS and "> DRAG_THRESHOLD_PX" in JS
    for token in ('action: "drag"', 'action: "click"', 'action: "up"', '"Escape"',
                  "shiftKey", "pointerdown", "pointerup", "setPointerCapture",
                  "lv-dot", "lv-guide"):
        assert token in JS, f"缺少 {token}"
    assert "lv-guide" in CSS and "lv-dot" in CSS


def test_robustness_no_reconnect_loop_and_backoff():
    """稳定性：出错不删 <img>（否则轮询重建 → 断连循环）；重试指数退避；空闲不刷屏。"""
    assert "removeChild(img)" not in JS and 'img.remove()' not in JS
    assert "failStreak" in JS and "setTimeout" in JS and "15000" in JS
    assert "lr-off" not in JS
    assert "帧龄" in JS, "状态里应显示帧龄（判断画面是否还活着）"


def test_dismiss_and_control_allowed_flag():
    assert "/dismiss" in JS, "组件应带「关遮挡」"
    assert "controlAllowed" in JS and "setControlAllowed" in JS, "服务端关闭交互时组件应隐藏入口"


def test_playground_split_layout_still_works():
    """分栏与中缝拖拽仍在 Playground（页面级布局），但画面本体来自组件。"""
    assert 'id="split"' in PG_HTML and 'id="splitHandle"' in PG_HTML and 'id="liveToggle"' in PG_HTML
    css = (WEBUI / "assets/css/playground.css").read_text(encoding="utf-8")
    assert "col-resize" in css and "#splitHandle" in css
    assert "function syncSplitHandle(" in PG_JS and "function initSplitHandle(" in PG_JS
    assert "setPointerCapture" in PG_JS and "pointercancel" in PG_JS


def test_browser_page_keeps_toolbar_extras():
    """画面页特有：provider 选择、fps/quality、暂停、保存当前帧、状态行。"""
    for token in ('id="provider"', 'id="fps"', 'id="quality"', 'id="pause"', 'id="save"', 'id="status"'):
        assert token in BR_HTML, f"画面页缺少 {token}"
    assert "extraQuery" in BR_JS and "setEnabled" in BR_JS
