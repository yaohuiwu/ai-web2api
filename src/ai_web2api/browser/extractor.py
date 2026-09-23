"""DOM → Markdown 提取。

注入一段自包含 JS 到页面，把响应容器（渲染后的 HTML）转换为 Markdown 文本。
轮询拉取 + 前缀 diff 由 Provider 驱动完成。
"""

from __future__ import annotations

import json
import time

from playwright.async_api import Page

_MD_JS = r"""
(sel, index) => {
  const nodes = document.querySelectorAll(sel);
  const el = index < 0 ? nodes[nodes.length - 1] : nodes[index];
  if (!el) return "";
  const blocks = [];
  const walkChildren = (node, list) => {
    [...node.childNodes].forEach((child) => {
      if (child.nodeType === 3) {
        const t = child.textContent;
        if (!t) return;
        if (!/\S/.test(t)) {                // 纯空白
          if (!t.includes("\n")) list.push(" "); // 行内空白→单空格
          return;                          // 含换行的源码格式空白→丢弃
        }
        list.push(t.replace(/[ \t\r\n]+/g, " ")); // 折叠（同 HTML 渲染）
        return;
      }
      if (child.nodeType === 1) walk(child, list);
    });
  };
  const seenImgs = new Set();
  const pushImg = (node, list) => {
    const altRaw = node.getAttribute ? (node.getAttribute("alt") || "") : "";
    let src = node.getAttribute ? (node.getAttribute("src") || node.src || "") : (node.src || "");
    let alt = altRaw;
    // ChatGPT 把原图 URL 塞进 alt（purpose=fullsize），src 是缩略图（purpose=inline）
    // → 优先用原图；alt 是 URL 的一律清掉，避免 `![http...](http...)` 垃圾
    if (/^https?:\/\//.test(alt)) {
      if (src && !/purpose=inline/.test(src)) { alt = ""; }
      else { src = alt; alt = ""; }
    }
    if (!src) { return; }
    if (seenImgs.has(src)) { return; }
    seenImgs.add(src);
    list.push("![" + alt + "](" + src + ")");
  };
  const walk = (node, list) => {
    const tag = node.tagName.toLowerCase();
    const cls = typeof node.className === "string" ? node.className : "";
    if (node.style && node.style.display === "none") return;
    if (node.getAttribute && node.getAttribute("aria-hidden") === "true") return;
    if (cls.includes("katex")) {
      const ann = node.querySelector(".katex-mathml annotation");
      const tex = ann ? ann.textContent.trim() : node.textContent.trim();
      if (tex) list.push("$" + tex + "$");
      return;
    }
    if (tag === "pre") {
      const code = node.querySelector("code");
      const text = (code || node).textContent.replace(/\n+$/, "");
      let lang = "";
      if (code) {
        const l = [...code.classList].find((c) => c.startsWith("language-"));
        if (l) lang = l.slice(9);
      }
      list.push("```" + lang + "\n" + text + "\n```");
      return;
    }
    if (tag === "code") { list.push("`" + node.textContent + "`"); return; }
    if (["h1","h2","h3","h4","h5","h6"].includes(tag)) {
      list.push("\n" + "#".repeat(Number(tag[1])) + " " + node.textContent.trim() + "\n");
      return;
    }
    if (tag === "p") { walkChildren(node, list); list.push("\n\n"); return; }
    if (tag === "blockquote") {
      const inner = [];
      walkChildren(node, inner);
      list.push("> " + inner.join("").trim().replace(/\n+/g, "\n> ") + "\n");
      return;
    }
    if (tag === "ul" || tag === "ol") {
      const ordered = tag === "ol";
      [...node.children].forEach((li, i) => {
        const inner = [];
        walkChildren(li, inner);
        list.push((ordered ? i + 1 + "." : "-") + " " + inner.join("").trim() + "\n");
      });
      list.push("\n");
      return;
    }
    if (tag === "table") {
      const rows = [...node.querySelectorAll("tr")].map((tr) =>
        [...tr.querySelectorAll("th,td")].map((c) => c.textContent.trim())
      );
      if (rows.length) {
        rows.forEach((r, i) => {
          list.push("| " + r.join(" | ") + " |\n");
          if (i === 0) list.push("|" + r.map(() => "---").join("|") + "|\n");
        });
        list.push("\n");
      }
      return;
    }
    if (tag === "br") { list.push("\n"); return; }
    if (tag === "button") {   // UI 按钮（收藏/复制/重生成…）：只留图片，丢弃按钮文案
      node.querySelectorAll("img").forEach((im) => pushImg(im, list));
      return;
    }
    if (tag === "img") { pushImg(node, list); return; }
    if (tag === "iframe") {   // 交互组件（如 Kimi 的 kimi-canvas 地图/小部件）→ 至少给出入口
      const src = node.getAttribute("src") || "";
      if (src) list.push("\n[🧩 交互组件](" + src + ")\n");
      return;
    }
    if (["strong","b"].includes(tag)) { list.push("**" + node.textContent + "**"); return; }
    if (["em","i"].includes(tag)) { list.push("*" + node.textContent + "*"); return; }
    if (tag === "a") { list.push("[" + node.textContent + "](" + node.href + ")"); return; }
    walkChildren(node, list);
  };
  walk(el, blocks);
  const out = blocks.join("");
  // 规范化空白：``` 代码块内保留原样，其余按 HTML 语义折叠行首/行尾空白与多余空行
  const parts = out.split("```");
  for (let i = 0; i < parts.length; i += 2) {
    parts[i] = parts[i]
      .replace(/^[ \t]+/gm, "")
      .replace(/[ \t]+$/gm, "")
      .replace(/\n{3,}/g, "\n\n");
  }
  return parts.join("```").trim();
}
"""


async def count_matches(page: Page, selector: str) -> int:
    """统计页面中匹配 selector 的元素个数。

    用 Playwright locator 实现：同时支持纯 CSS 与 Playwright 扩展语法
    （:has-text()、text= 等）。注意 extract_markdown 的 JS 提取仍要求
    纯 CSS，响应容器选择器请保持 CSS 写法。
    """
    try:
        return await page.locator(selector).count()
    except Exception:
        return 0


async def first_match(page: Page, selectors: list[str]) -> str | None:
    """候选列表里第一个在页面上存在的选择器；全部不匹配返回 None。"""
    for s in selectors:
        if await count_matches(page, s) > 0:
            return s
    return None


async def wait_first_match(
    page: Page,
    selectors: list[str],
    *,
    timeout: float = 15.0,
    poll: float = 0.3,
) -> str | None:
    """轮询等待候选列表里任一选择器出现；超时返回 None。

    SPA 在 ``domcontentloaded`` 之后才异步渲染 DOM，直接 ``first_match``
    会拿到空的登录表单/聊天页。用这个函数给渲染留出时间。
    """
    deadline = time.monotonic() + timeout
    while True:
        sel = await first_match(page, selectors)
        if sel is not None:
            return sel
        if time.monotonic() >= deadline:
            return None
        await page.wait_for_timeout(poll * 1000)


async def extract_markdown(page: Page, selector: str | None, index: int | None = -1) -> str:
    """提取第 index 个（默认最后一个）匹配元素为 Markdown；无匹配返回空串。"""
    if not selector:
        return ""
    return await page.evaluate(f"({_MD_JS})({json.dumps(selector)}, {index if index is not None else -1})")


async def extract_markdown_parts(page: Page, selector: str | None, start: int) -> list[str]:
    """从第 ``start`` 个匹配元素起，逐个提取（**不去重、不过滤**），供调用方自行筛选。

    典型用途：拼接多容器回答时，按内容剔除"其实是思考"的块。
    """
    if not selector:
        return []
    count = await count_matches(page, selector)
    if count <= 0:
        return []
    parts: list[str] = []
    for i in range(max(0, min(start, count)), count):
        text = (await extract_markdown(page, selector, i)).strip()
        if text:
            parts.append(text)
    return parts


async def extract_markdown_from(page: Page, selector: str | None, start: int) -> str:
    """从第 ``start`` 个匹配元素起，把**后面的全部**依次提取并拼接（空行分隔）。

    为什么需要：一次回答可能被站点拆成**多条消息 / 多个容器**（如 Kimi 工具调用会把回答拆成
    "工具前的一段 + 工具后的一段"）。只读"新增的第一个容器"会截断内容。
    相邻重复内容会被去重（容器重渲染时的兜底）。
    """
    if not selector:
        return ""
    parts: list[str] = []
    for text in await extract_markdown_parts(page, selector, start):
        if not parts or parts[-1] != text:      # 去重（容器重渲染兜底）
            parts.append(text)
    return "\n\n".join(parts)


# 「消息已完成」工具栏检测用的 JS：
#   - 只认**当前这条**回复（最后一个匹配 response_container 的元素）所在的消息块，
#     否则第一条消息的工具栏会一直命中（误判"已完成"）；
#   - 工具栏存在但**被隐藏**（仅 hover 才显示）不算 → 最多向上 5 层检查 display/visibility/opacity。
_DONE_TOOLBAR_JS = """
(args) => {
  const [respSels, toolSels, maxDepth] = args;
  let el = null;
  for (const s of respSels) {
    try { const n = document.querySelectorAll(s); if (n.length) { el = n[n.length - 1]; break; } }
    catch (e) { /* 非法选择器忽略 */ }
  }
  if (!el) return false;
  const visible = (e) => {
    for (let n = e, i = 0; n && i < 4; n = n.parentElement, i++) {
      const st = getComputedStyle(n);
      if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
    }
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  for (let node = el, i = 0; node && i <= maxDepth; node = node.parentElement, i++) {
    for (const t of toolSels) {
      let hits = [];
      try { hits = node.querySelectorAll(t); } catch (e) { continue; }
      for (const h of hits) if (visible(h)) return true;
    }
  }
  return false;
}
"""


async def has_done_toolbar(
    page: Page,
    response_selectors: list[str],
    toolbar_selectors: list[str],
    max_depth: int = 5,
) -> bool:
    """**当前这条**回复所在的消息块里，是否已出现"消息工具栏"（复制/点赞/朗读…）。

    这是站点自己给出的"该条消息已完成"信号（实测 ChatGPT：工具栏出现 == 停止按钮消失
    == 站点认为本轮结束）。用于没有停止按钮的站点（如豆包）作为结束判据。

    - 只在**最后一个** ``response_container`` 所在的祖先子树里找 → 不会命中历史消息；
    - 工具栏存在但被隐藏（仅 hover 显示）不算完成。
    """
    if not response_selectors or not toolbar_selectors:
        return False
    try:
        return bool(
            await page.evaluate(
                _DONE_TOOLBAR_JS, [list(response_selectors), list(toolbar_selectors), max_depth]
            )
        )
    except Exception:  # noqa: BLE001  页面导航/元素失效等 → 当作"没有信号"，由稳定性兜底
        return False
