"""DeepSeek Web (chat.deepseek.com) 驱动。

通用聊天流程（开页/选项/附件/发送/轮询/增量）在 :class:`WebChatProvider`；
本文件只保留 DeepSeek 专属部分：

- 会话 URL 形态 ``/a/chat/s/<uuid>``
- 旧 mode 值 → 新版开关的翻译（``MODE_PRESETS``）
- 响应解析（SSE JSON-Patch 协议，``_parse_sse_snapshot``）
- 思考面板提取（"已思考（用时 N 秒）"）

网络抓取（XHR 监听 ``/api/v0/chat/completion``）由基类按 ``network.url_pattern``
生成（见 ``config.yaml``），不再写死在驱动里。
"""

from __future__ import annotations

import json
import logging

from playwright.async_api import Page

from .webchat import WebChatProvider

logger = logging.getLogger(__name__)


class DeepSeekProvider(WebChatProvider):
    """chat.deepseek.com 驱动。"""

    name = "deepseek"
    # 会话 URL: chat.deepseek.com/a/chat/s/<uuid>（末段即 DeepSeek 会话 id，
    # 服务端保存用户会话不回收 → thread 持久化恢复可靠）
    session_url_pattern = r"/a/chat/s/([0-9a-fA-F-]{8,})"

    # 旧版 UI 的三模式（快速/专家/识图）在新版 UI 已合并为单一模式，请求体只剩
    # thinking_enabled / search_enabled 两个开关。这里把旧 mode 值翻译成开关组合，
    # 让老客户端（和 playground 的 mode 预设）继续可用；显式 deep_think/search 参数优先。
    MODE_PRESETS: dict[str, dict[str, bool]] = {
        "fast": {"deep_think": False, "search": False},
        "expert": {"deep_think": True, "search": True},
        "image": {"deep_think": False, "search": False},
    }

    MAX_ATTACHMENTS = 50  # DeepSeek tooltip：最多 50 个
    MAX_ATTACH_BYTES = 100 * 1024 * 1024  # 每个最大 100MB
    ATTACH_PREVIEW_EXCLUDE = ".ds-message"  # 预览计数排除消息区历史图片

    # ---------- 网络响应解析（DeepSeek JSON-Patch 协议） ----------

    @staticmethod
    def _apply_sse_op(state: dict, op: dict, base: str = "") -> None:
        """应用一条 SSE message 的 JSON Patch 操作到 state（可变）。

        支持 DeepSeek 实测的操作形态：
        - 完整快照：{"v": {"response": {...}}}（首帧，无 p/o）
        - 全量 SET：{"p":"response","o":"SET","v":{...}}
        - 嵌套批量：{"p":"response","o":"BATCH","v":[{...}, ...]}（子操作路径相对父路径）
        - 数组追加：{"p":"response/fragments","o":"APPEND","v":[{...fragment}]}
        - 字段追加：{"p":"response/fragments/-1","o":"BATCH","v":[{"p":"content","o":"APPEND","v":"文本"}]}
        - 字段赋值：{"p":"response/fragments/-1/references","o":"SET","v":[...]}
        """
        o = op.get("o")
        if o is None:
            # DeepSeek 实测：content 增量操作常省略 o 字段（默认追加）
            o = "APPEND"
        p = (op.get("p") or "").strip("/")
        if p.startswith("response"):
            full = p  # 绝对路径
        elif base and p:
            full = f"{base}/{p}"
        else:
            full = base or p
        if o == "BATCH":
            for sub in op.get("v") or []:
                if isinstance(sub, dict):
                    DeepSeekProvider._apply_sse_op(state, sub, full)
            return
        v = op.get("v")
        if not full:
            if isinstance(v, dict) and isinstance(v.get("response"), dict):
                # 完整快照（message 首帧/响应 SET 全量）
                state["response"] = v["response"]
            elif isinstance(v, str) and v:
                # 无路径字符串增量（DeepSeek 实测：正文/思考逐字增量，无 p/o 字段）
                # 追加到当前（最后一个）fragment 的 content
                resp = state.get("response")
                if isinstance(resp, dict):
                    frags = resp.get("fragments")
                    if isinstance(frags, list) and frags:
                        cur = frags[-1]
                        cur["content"] = cur.get("content", "") + v
            return
        segs = full.split("/")
        node: dict | list | None = state
        for s in segs[:-1]:
            if isinstance(node, list):
                try:
                    idx = int(s)
                except ValueError:
                    return
                node = node[idx] if -len(node) <= idx < len(node) else None
            elif isinstance(node, dict):
                node = node.get(s)
            else:
                node = None
            if node is None:
                return
        key = segs[-1]
        if o == "SET":
            if isinstance(node, list):
                try:
                    idx = int(key)
                except ValueError:
                    return
                if -len(node) <= idx < len(node):
                    node[idx] = v
            elif isinstance(node, dict):
                node[key] = v
        elif o == "APPEND":
            if isinstance(node, dict):
                cur = node.get(key)
                if isinstance(cur, list):
                    cur.extend(v) if isinstance(v, list) else cur.append(v)
                elif isinstance(cur, str):
                    node[key] = cur + (v or "")
                else:
                    node[key] = v
            elif isinstance(node, list):
                node.extend(v) if isinstance(v, list) else node.append(v)

    @classmethod
    def _parse_sse_snapshot(cls, sse_text: str) -> tuple[str, str]:
        """从 SSE 响应文本解析 (thinking, content)，thinking 含搜索资料块。

        逐个应用 message 事件的 JSON Patch 操作重建完整响应状态（首帧是完整
        快照，后续是 p/o/v 增量 patch），再从最终 fragments 按片段类型组装：
        - THINK → 思考文本
        - TOOL_SEARCH / TOOL_OPEN → 搜索资料（结构化：标题/网站/URL/摘要，URL 去重）
        - RESPONSE → 正文（含 [reference:N] 引用标记，原样保留）
        """
        if not sse_text:
            return "", ""
        state: dict = {}
        block_count = 0
        parsed_count = 0
        json_err_count = 0
        for block in sse_text.split("\n\n"):
            data = None
            for line in block.split("\n"):
                if line.startswith("data:"):
                    data = (data or "") + line[5:].strip()
            if not data:
                continue
            block_count += 1
            try:
                obj = json.loads(data)
            except Exception:
                json_err_count += 1
                continue
            parsed_count += 1
            cls._apply_sse_op(state, obj)
        resp = state.get("response")
        if not isinstance(resp, dict):
            logger.warning(
                "[%s] _parse_sse_snapshot: response不是dict, "
                "block_count=%d parsed_count=%d json_err_count=%d, "
                "state_keys=%r",
                cls.name, block_count, parsed_count, json_err_count,
                list(state.keys()) if isinstance(state, dict) else type(state),
            )
            return "", ""
        fragments = resp.get("fragments") or []
        logger.debug(
            "[%s] _parse_sse_snapshot: block_count=%d parsed=%d json_err=%d, "
            "fragments=%d, types=%r",
            cls.name, block_count, parsed_count, json_err_count,
            len(fragments),
            [f.get("type") for f in fragments],
        )
        thinking_parts: list[str] = []
        content_parts: list[str] = []
        search_block = ["【搜索资料】"]
        seen_urls: set[str] = set()
        for frag in fragments:
            ftype = frag.get("type")
            if ftype == "THINK":
                c = frag.get("content") or ""
                if c:
                    thinking_parts.append(c)
            elif ftype == "RESPONSE":
                c = frag.get("content") or ""
                if c:
                    content_parts.append(c)
            elif ftype in ("TOOL_SEARCH", "TOOL_OPEN"):
                raw = None
                if ftype == "TOOL_OPEN":
                    raw = frag.get("result")
                else:
                    for r in frag.get("results") or []:
                        if isinstance(r, dict):
                            raw = r
                            break
                if not isinstance(raw, dict) or not raw.get("title"):
                    continue
                url = raw.get("url") or ""
                if url and url in seen_urls:
                    continue
                seen_urls.add(url)
                title = raw.get("title") or ""
                site = raw.get("site_name") or ""
                snip = (raw.get("snippet") or "").strip()
                entry = f"{len(seen_urls)}. {title}"
                if site:
                    entry += f"（{site}）"
                if url:
                    entry += f"\n   {url}"
                if snip:
                    entry += f"\n   {snip}"
                search_block.append(entry)
        thinking = "\n\n".join(thinking_parts).strip()
        if len(search_block) > 1:
            thinking = (thinking + "\n\n" if thinking else "") + "\n".join(search_block)
        logger.debug(
            "[%s] _parse_sse_snapshot结果: thinking_len=%d content_len=%d",
            cls.name, len(thinking), len("\n\n".join(content_parts).strip()),
        )
        return thinking, "\n\n".join(content_parts).strip()

    # ---------- 思考面板提取 ----------

    # 思考提取：取第 index 个 think 元素所在的"已思考"折叠面板的完整文本。
    # DeepSeek 搜索模式下，搜索资料（"搜索到 N 个网页" + 浏览的页面标题列表）就在
    # 思考面板内部、与思考段交错排列，但它们是独立 hash class 容器，用 CSS 选择器
    # 无法定位——只能从 think 元素向上找面板再取 innerText。
    # 注意必须去掉"已思考（用时 N 秒）"标题：秒数在思考过程中每秒变化，留着会让
    # 整段文本每次都不是前缀/子串延续 → diff 丢增量 → 思考输出缺字。
    _THINK_PANEL_JS = r"""
(sel, index) => {
  const nodes = document.querySelectorAll(sel);
  if (!nodes.length) return "";
  const el = index < 0 ? nodes[nodes.length - 1] : nodes[index];
  if (!el) return "";
  let p = el;
  for (let i = 0; i < 5 && p; i++) {
    const t = (p.textContent || "").trim();
    if (/^已思考/.test(t)) break;
    p = p.parentElement;
  }
  if (!p) return (el.textContent || "").trim();
  return (p.innerText || "").replace(/^已思考（用时[\s\S]*?秒）\s*/, "").trim();
}
"""

    async def _extract_thinking(self, page: Page, selector: str, index: int | None) -> str:
        """提取思考面板文本（含搜索资料），无匹配返回空串。"""
        if not selector:
            return ""
        return await page.evaluate(
            f"({self._THINK_PANEL_JS})({json.dumps(selector)}, {index if index is not None else -1})"
        )
