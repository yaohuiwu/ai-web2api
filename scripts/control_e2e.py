"""端到端验证画面交互（点击/拖拽/输入/滚轮）：对**本地假站点**跑，不碰真站点。

用法：
    # 1) 起假站点（配置里 live_control: true, debug_port: 9333）
    AI_WEB2API_CONFIG=config.fake.yaml .venv/bin/python -m ai_web2api.main
    # 2) 预热一个会话（让页面存在）
    curl -s -X POST http://127.0.0.1:8001/v1/chat/completions -H 'Content-Type: application/json' \
      -d '{"model":"fake-web","thread_id":"e2e-1","messages":[{"role":"user","content":"你好"}]}'
    # 3) 跑本脚本（读页面 window.__probe() 断言真实输入事件）
    .venv/bin/python scripts/control_e2e.py

覆盖：真实点击（含 hover）、拖拽起点/终点坐标、点输入框+打字+Enter、滚轮滚动、重置输入(up+Esc)。
"""
import asyncio, json, urllib.request
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8001"


def post_input(payload, tid="e2e-1"):
    req = urllib.request.Request(f"{BASE}/admin/fake/input?thread_id={tid}",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.connect_over_cdp("http://127.0.0.1:9333")
        page = next(p for c in b.contexts for p in c.pages if "fake_chat" in p.url)
        print("页面:", page.url.split("/")[-1], "视口:", await page.evaluate("() => [innerWidth, innerHeight]"))

        async def probe():
            return await page.evaluate("() => window.__probe ? window.__probe() : []")

        async def clear():
            await page.evaluate("() => { window.__probe().length = 0; }")

        # ① 点击探针按钮（坐标由页面给出，模拟前端换算）
        btn = await page.locator("#probe-btn").bounding_box()
        r1 = post_input({"action": "click", "x": round(btn["x"] + btn["width"] / 2),
                         "y": round(btn["y"] + btn["height"] / 2)})
        await asyncio.sleep(0.3)
        print("① click:", r1.get("mapped"), "→ 探针:", await probe())

        # ② 拖拽（起点=拖拽框中心，终点 +60/+40）
        await clear()
        db = await page.locator("#dragbox").bounding_box()
        cx, cy = db["x"] + db["width"] / 2, db["y"] + db["height"] / 2
        r2 = post_input({"action": "drag", "x": round(cx), "y": round(cy),
                         "x2": round(cx + 60), "y2": round(cy + 40), "steps": 8, "delay_ms": 10})
        await asyncio.sleep(0.4)
        print("② drag:", r2.get("mapped"), "steps:", r2.get("steps"), "→ 探针:", await probe())

        # ③ 输入 + Enter（点击输入框 → 打字 → 回车）
        await clear()
        inp = await page.locator("#chat-input").bounding_box()
        post_input({"action": "click", "x": round(inp["x"] + 40), "y": round(inp["y"] + 20)})
        r3 = post_input({"action": "type", "text": "端到端测试ABC"})
        post_input({"action": "key", "key": "Enter"})
        await asyncio.sleep(1.0)
        msgs = await page.evaluate("() => document.getElementById('messages').innerText")
        print("③ type chars:", r3.get("chars"), "→ 页面消息含:", "端到端测试ABC" in msgs, repr(msgs[:60]))

        # ④ 滚轮：向页面发 wheel，滚动区域应记录 scroll
        await clear()
        sa = await page.locator("#scroll-area").bounding_box()
        post_input({"action": "wheel", "x": round(sa["x"] + 20), "y": round(sa["y"] + 30), "dy": 300})
        await asyncio.sleep(0.4)
        print("④ wheel → 探针:", await probe())

        # ⑤ 重置输入（up + Esc）不应报错
        print("⑤ reset(up):", post_input({"action": "up", "x": 10, "y": 10}).get("ok"),
              "esc:", post_input({"action": "key", "key": "Escape"}).get("ok"))
        await b.close()


asyncio.run(main())
