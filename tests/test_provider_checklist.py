"""路线 1「Provider 接入检查清单」的静态守卫（见 docs/EXTRACTION_STRATEGY.md §3.5）。

运行：.venv/bin/python -m pytest tests/test_provider_checklist.py -v
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_web2api.config import load_config

ROOT = Path(__file__).resolve().parent.parent
CFG = load_config(ROOT / "config.yaml")
ENABLED = [p for p in CFG.providers if p.enabled]
DISABLED = [p for p in CFG.providers if not p.enabled]


def test_there_are_enabled_providers():
    assert ENABLED, "至少应有一个启用的 provider"


@pytest.mark.parametrize("p", ENABLED, ids=lambda p: p.name)
def test_containers_and_input_configured(p):
    """① 只匹配助手侧的容器 ③ 输入框必须配好（否则根本聊不了）。"""
    assert p.selectors.input, f"{p.name}: 未配置输入框选择器"
    assert p.selectors.response_container, f"{p.name}: 未配置正文容器选择器"


@pytest.mark.parametrize("p", ENABLED, ids=lambda p: p.name)
def test_contenteditable_requires_typing(p):
    """contenteditable 必须逐字输入（fill() 对 React 受控组件常无效）。"""
    joined = " ".join(p.selectors.input)
    if "contenteditable" in joined:
        assert p.selectors.type_prompt is True, f"{p.name}: contenteditable 需 type_prompt: true"


@pytest.mark.parametrize("p", ENABLED, ids=lambda p: p.name)
def test_stop_button_has_settle_polls(p):
    """⑤ 配了停止按钮就必须有稳定确认（防"按钮一消失就取文本"截断）。"""
    if p.selectors.stop_button:
        assert p.selectors.stop_settle_polls >= 1, f"{p.name}: 停止按钮需 stop_settle_polls >= 1"


@pytest.mark.parametrize("p", ENABLED, ids=lambda p: p.name)
def test_render_heavy_sites_buffer_or_join(p):
    """②③ 重渲染严重的站点至少要开一个：缓冲后发 或 多容器拼接。"""
    assert p.selectors.response_all_new or not p.selectors.stream_content, (
        f"{p.name}: 建议 stream_content: false 或 response_all_new: true（防丢内容/截断）"
    )


def test_disabled_providers_keep_calibration():
    """禁用的 provider 也应保留已校准的选择器（需要时一键启用，不必重新校准）。"""
    for p in DISABLED:
        assert p.selectors.input or p.login.detect or p.selectors.logged_out, (
            f"{p.name}: 禁用但配置完全空白（校准成果丢失？）"
        )
