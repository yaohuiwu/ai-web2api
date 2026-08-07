"""DOM → Markdown 提取。

注入一段自包含 JS 到页面，把响应容器（渲染后的 HTML）转换为 Markdown 文本。
轮询拉取 + 前缀 diff 由 Provider 驱动完成。
"""

from __future__ import annotations

import json

from playwright.async_api import Page

_MD_JS = r"""
(sel, index) => {
  const nodes = document.querySelectorAll(sel);
  const el = index < 0 ? nodes[nodes.length - 1] : nodes[index];
  if (!el) return "";
  const blocks = [];
  const walkChildren = (node, list) => {
    [...node.childNodes].forEach((child) => {
      if (child.nodeType === 3) { list.push(child.textContent); return; }
      if (child.nodeType === 1) walk(child, list);
    });
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
    if (tag === "p") { walkChildren(node, list); list.push("\n"); return; }
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
    if (tag === "img") { list.push("![" + (node.alt || "") + "](" + (node.src || "") + ")"); return; }
    if (["strong","b"].includes(tag)) { list.push("**" + node.textContent + "**"); return; }
    if (["em","i"].includes(tag)) { list.push("*" + node.textContent + "*"); return; }
    if (tag === "a") { list.push("[" + node.textContent + "](" + node.href + ")"); return; }
    walkChildren(node, list);
  };
  walk(el, blocks);
  return blocks.join("").replace(/\n{3,}/g, "\n\n").trim();
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


async def extract_markdown(page: Page, selector: str | None, index: int | None = -1) -> str:
    """提取第 index 个（默认最后一个）匹配元素为 Markdown；无匹配返回空串。"""
    if not selector:
        return ""
    return await page.evaluate(f"({_MD_JS})({json.dumps(selector)}, {index if index is not None else -1})")
