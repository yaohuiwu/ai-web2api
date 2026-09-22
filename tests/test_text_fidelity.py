"""文本完整性回归（live）：我们返回的正文 vs **页面上真实可见的文本**。

度量：① 句子覆盖率（页面句子有多少出现在正文里）② 思考泄漏 ③ 长度。
页面侧只读 ``innerText``（刻意不用我们的 markdown 提取器），尽量独立。

运行（需已启动服务 + 已登录；Kimi 排队时会 skip）：
    .venv/bin/python -m pytest -m live tests/test_text_fidelity.py -v
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
import re
import urllib.error
import urllib.request

import pytest

BASE = "http://127.0.0.1:8000"
CASES = [
    ("long", "分 5 点介绍中国的世界遗产，每点 2-3 句话，用 markdown 列表"),
    ("code", "用 Python 写一个快速排序函数，带中文注释，必须放在代码块里"),
    ("table", "用 markdown 表格对比快速排序、归并排序、堆排序的时间/空间复杂度，3 列"),
]
PROVIDERS = [
    ("deepseek-web", "deepseek"),
    ("gpt-5-web", "chatgpt"),
    ("kimi-web", "kimi"),
    ("doubao-web", "doubao"),
    ("glm-web", "glm"),
]
REF_SEL = {
    "deepseek": ".ds-assistant-message-main-content",
    "chatgpt": '[data-message-author-role="assistant"] .markdown',
    "kimi": ".chat-content-item-assistant .markdown-container:not(.toolcall-content-text) .markdown",
    "doubao": '[data-container-type="block-v2"] > div:not([class*="justify-end"]) .md-box-root',
    "glm": ".markdown-body:not(.thinking-content *)",
}
THINK_SEL = {
    "deepseek": ".ds-think-content",
    "chatgpt": "",
    "kimi": ".markdown-container.toolcall-content-text",
    "doubao": "",
    "glm": ".thinking-content .markdown-body",
}

pytestmark = pytest.mark.live

# 思考"泄漏"的判据用**推理特征词**，而不是"与思考文本重合"：
# 代码/表格类任务里，思考中草拟的内容与正文本来就相同（重合是正常的）。
LEAK_RE = re.compile(
    r"正在思考中|思考已完成|已思考（用时|^(The user says|We need|I need to|I will|I'll|Let me|First,? I)",
    re.IGNORECASE | re.MULTILINE,
)


def _norm(s: str) -> str:
    """比对用归一化：只留字母/数字/汉字（markdown 标记与空白都不算内容）。"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", s or "")


def _sentences(s: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?;\n])", s or "")]
    return [p for p in parts if len(_norm(p)) >= 6]


def _coverage(reference: str, output: str) -> tuple[float, list[str]]:
    sents = _sentences(reference)
    if not sents:
        return 1.0, []
    missing = [s for s in sents if _norm(s)[:24] not in _norm(output)]
    return 1 - len(missing) / len(sents), [s[:30] for s in missing[:3]]


def _post(model: str, text: str, thread_id: str) -> dict:
    body = {"model": model, "thread_id": thread_id, "messages": [{"role": "user", "content": text}],
            "deep_think": True}
    req = urllib.request.Request(f"{BASE}/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:  # 429 = provider 串行闸门/队列满；504 = 站点排队
        pytest.skip(f"{model} 暂不可用（HTTP {e.code}）")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"{model} 请求失败：{e}")
    if "choices" not in data:
        pytest.skip(f"{model} 生成失败：{(data.get('error') or {}).get('message')}")
    return data["choices"][0]["message"]


def _page_url(thread_id: str) -> str:
    try:
        with urllib.request.urlopen(f"{BASE}/admin/threads?q={thread_id}&limit=1", timeout=20) as r:
            items = json.loads(r.read()).get("threads") or []
    except Exception:  # noqa: BLE001
        return ""
    return (items[0].get("page_url") or "") if items else ""


async def _read_page(provider: str, url: str) -> tuple[str, str]:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            storage_state=f"profiles/{provider}/state.json",
            locale="en-US" if provider == "chatgpt" else "zh-CN",
            viewport={"width": 1440, "height": 900},
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(10000)
        ref = await page.evaluate(
            "(sel) => { const n = document.querySelectorAll(sel); const el = n[n.length-1];"
            " return el ? (el.innerText || el.textContent || '') : ''; }",
            REF_SEL[provider],
        )
        think = ""
        if THINK_SEL[provider]:
            think = await page.evaluate(
                "(sel) => { const n = document.querySelectorAll(sel); const el = n[n.length-1];"
                " return el ? (el.innerText || el.textContent || '') : ''; }",
                THINK_SEL[provider],
            )
        await browser.close()
        return ref, think


REPORT = Path(os.environ.get("TEXT_FIDELITY_REPORT", "reports/text_fidelity.jsonl"))


def _record(entry: dict) -> None:
    """把每条用例结果追加到 JSONL（趋势/告警用；失败也记录）。"""
    try:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        with REPORT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001  记录失败不影响测试结论
        pass


@pytest.mark.parametrize("model,provider", PROVIDERS)
@pytest.mark.parametrize("tag,prompt", CASES)
@pytest.mark.asyncio
async def test_text_fidelity(model: str, provider: str, tag: str, prompt: str):
    """正文必须覆盖页面可见文本（≥95% 句子命中），且不得把思考当正文。"""
    thread_id = f"fid-{provider}-{tag}"
    message = _post(model, prompt, thread_id)
    content = message.get("content") or ""
    url = _page_url(thread_id)
    if not url:
        pytest.skip(f"{provider} 未记录会话 URL（无法读页面比对）")
    reference, thinking = await _read_page(provider, url)

    coverage, missing = _coverage(reference, content)
    leaked = LEAK_RE.search(content.strip()[:200])
    _record({
        "ts": round(time.time()), "provider": provider, "model": model, "case": tag,
        "coverage": round(coverage, 3), "content_len": len(content),
        "reasoning_len": len(message.get("reasoning_content") or ""),
        "leak": leaked.group(0) if leaked else None, "missing": missing,
    })
    assert coverage >= 0.95, f"[{provider}/{tag}] 覆盖率 {coverage:.2f}，缺 {missing}"
    assert not leaked, f"[{provider}/{tag}] 正文里混进了思考特征文本：{leaked.group(0)!r}"
