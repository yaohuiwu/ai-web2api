# chatgpt_stream.py
import asyncio
import sys
import time
from playwright.async_api import async_playwright


# ============================================================
# 注入到页面上下文的 MutationObserver 脚本
# 关键点：characterData=True 捕获文本节点级增量
# ============================================================
OBSERVER_SCRIPT = r"""
(function() {
    if (window.__chatgptObserver) {
        window.__chatgptObserver.disconnect();
    }

    function findAssistantContainer() {
        const selectors = [
            '[data-message-author-role="assistant"]',
            '[data-testid^="conversation-turn-"] [data-message-author-role="assistant"]',
            '.agent-turn',
        ];
        for (const sel of selectors) {
            const nodes = document.querySelectorAll(sel);
            if (nodes.length > 0) return nodes[nodes.length - 1];
        }
        return null;
    }

    let container = findAssistantContainer();
    if (!container) return 'NO_CONTAINER';

    // 防抖：避免流式高频变化刷屏
    let debounceTimer = null;
    const DEBOUNCE_MS = 80;

    function emit() {
        const text = container ? (container.innerText || '') : '';
        if (window.__onStreamChunk) {
            window.__onStreamChunk({ fullText: text });
        }
    }

    const observer = new MutationObserver((mutations) => {
        for (const mutation of mutations) {
            // 文本节点内容变化 = 流式输出的核心信号
            if (mutation.type === 'characterData') {
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(emit, DEBOUNCE_MS);
            }
            // 子节点增删 = 检测新的回复容器 / 新块插入
            if (mutation.type === 'childList') {
                const newContainer = findAssistantContainer();
                if (newContainer && newContainer !== container) {
                    container = newContainer;
                    observer.disconnect();
                    observer.observe(container, {
                        childList: true,
                        subtree: true,
                        characterData: true,
                    });
                }
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(emit, DEBOUNCE_MS);
            }
        }
    });

    observer.observe(container, {
        childList: true,
        subtree: true,
        characterData: true,   // ← 增量文字流的关键
    });

    window.__chatgptObserver = observer;
    return 'OBSERVER_INSTALLED';
})();
"""


async def capture_stream(
    prompt: str,
    headless: bool = False,
    user_data_dir: str = "./chatgpt-profile",
) -> str:
    """
    发送 prompt，实时抓取 ChatGPT 的流式回复。

    :param prompt: 输入的问题
    :param headless: 是否无头模式（首次登录建议 False）
    :param user_data_dir: 持久化用户目录，保留登录态
    :return: 最终完整回复文本
    """
    async with async_playwright() as p:
        # 使用持久化上下文，保留登录状态，避免每次重新登录
        context = await p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else await context.new_page()

        # 创建 CDP 会话
        cdp = await context.new_cdp_session(page)

        # 状态变量
        last_text = ""
        final_text = ""
        last_change_ts = time.monotonic()
        done_event = asyncio.Event()

        # ---------- 浏览器 → Python 的回调 ----------
        async def on_stream_chunk(payload: dict):
            nonlocal last_text, final_text, last_change_ts
            full = payload.get("fullText", "") or ""

            if full != last_text:
                # 计算增量：优先用前缀匹配，回退到整体重发
                delta = full[len(last_text):] if full.startswith(last_text) else full
                if delta:
                    sys.stdout.write(delta)
                    sys.stdout.flush()
                last_text = full
                final_text = full
                last_change_ts = time.monotonic()

        await page.expose_function("__onStreamChunk", on_stream_chunk)

        # ---------- 页面交互（Playwright 负责） ----------
        await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        # 等待输入框出现（已登录状态）
        await page.wait_for_selector("#prompt-textarea", timeout=60_000)

        await page.fill("#prompt-textarea", prompt)
        await page.keyboard.press("Enter")

        # 等待助手回复容器挂载
        await page.wait_for_selector(
            '[data-message-author-role="assistant"]',
            timeout=60_000,
        )

        # ---------- CDP 注入监听器（底层负责） ----------
        result = await cdp.send(
            "Runtime.evaluate",
            {"expression": OBSERVER_SCRIPT, "returnByValue": True},
        )
        status = result.get("result", {}).get("value")
        print(f"\n[observer] {status}\n", flush=True)

        # ---------- 完成判定：文本稳定 1.5s 且停止按钮消失 ----------
        async def stability_watcher():
            while not done_event.is_set():
                await asyncio.sleep(0.5)
                if last_text and (time.monotonic() - last_change_ts) > 1.5:
                    stop_visible = await page.evaluate(
                        "() => !!document.querySelector("
                        "'button[data-testid=\"stop-button\"], "
                        "button[aria-label=\"Stop streaming\"]')"
                    )
                    if not stop_visible:
                        done_event.set()
                        return

        watcher = asyncio.create_task(stability_watcher())

        try:
            await asyncio.wait_for(done_event.wait(), timeout=180)
        except asyncio.TimeoutError:
            print("\n[timeout] 流式回复超过 180s 未完成", flush=True)
        finally:
            watcher.cancel()

        # ---------- 清理 ----------
        await cdp.send(
            "Runtime.evaluate",
            {
                "expression": (
                    "window.__chatgptObserver && "
                    "window.__chatgptObserver.disconnect()"
                )
            },
        )
        await context.close()
        return final_text


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    reply = asyncio.run(capture_stream("用三句话解释什么是量子纠缠", False, user_data_dir="../profiles/chatgpt/state.json"))
    print("\n\n===== 最终回复 =====")
    print(reply)