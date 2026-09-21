// 轻量 markdown 渲染（自包含，无 CDN 依赖；先 esc 防 XSS，再解析标记）
function mdInline(s) {
  s = esc(s);
  // esc 之后注入 <br>（多行段落/引用由调用方用 \n 连接）
  s = s.replace(/\n/g, "<br>");
  // 行内代码（最先，避免 code 内容被二次解析）
  s = s.replace(/`([^`\n]+)`/g, (m, c) => `<code>${c}</code>`);
  // 链接：仅 http(s) 白名单
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    (m, t, u) => `<a href="${esc(u)}" target="_blank" rel="noopener">${t}</a>`);
  // 粗体 / 斜体 / 删除线
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  return s;
}

function md(src) {
  src = String(src ?? "").replace(/\r\n/g, "\n");
  const lines = src.split("\n");
  const out = [];
  let i = 0;
  const isList = /^\s*(?:[-*+]|\d+\.)\s+/;
  while (i < lines.length) {
    const line = lines[i];
    // 围栏代码块
    const fence = line.match(/^```([\w+-]*)\s*$/);
    if (fence) {
      const buf = [];
      i++;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++; // 跳过闭合围栏
      out.push(`<pre><code${fence[1] ? ` class="lang-${esc(fence[1])}"` : ""}>${esc(buf.join("\n"))}</code></pre>`);
      continue;
    }
    // 表格：当前行 |...| 且下一行是分隔行
    if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const header = cells(lines[i]);
      const aligns = lines[i + 1].trim().replace(/^\||\|$/g, "").split("|").map((c) => {
        const t = c.trim();
        if (t.startsWith(":") && t.endsWith(":")) return " style=\"text-align:center\"";
        if (t.endsWith(":")) return " style=\"text-align:right\"";
        return "";
      });
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(cells(lines[i++]));
      let html = "<table><thead><tr>";
      header.forEach((c, idx) => { html += `<th${aligns[idx] || ""}>${mdInline(c)}</th>`; });
      html += "</tr></thead><tbody>";
      for (const r of rows) {
        html += "<tr>";
        r.forEach((c, idx) => { html += `<td${aligns[idx] || ""}>${mdInline(c)}</td>`; });
        html += "</tr>";
      }
      out.push(html + "</tbody></table>");
      continue;
    }
    // 标题
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      const n = h[1].length;
      out.push(`<h${n}>${mdInline(h[2])}</h${n}>`);
      i++;
      continue;
    }
    // 引用
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      out.push(`<blockquote>${mdInline(buf.join("\n"))}</blockquote>`);
      continue;
    }
    // 列表（无序/有序，单层）
    if (isList.test(line)) {
      const ordered = /^\s*\d+\.\s+/.test(line);
      const items = [];
      while (i < lines.length && isList.test(lines[i])) {
        items.push(lines[i].replace(/^\s*(?:[-*+]|\d+\.)\s+/, ""));
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.map((it) => `<li>${mdInline(it)}</li>`).join("")}</${tag}>`);
      continue;
    }
    // 空行
    if (!line.trim()) { i++; continue; }
    // 段落（GFM：单换行 → <br>）
    const buf = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !/^```/.test(lines[i])
      && !/^(#{1,4})\s/.test(lines[i]) && !/^\s*\|/.test(lines[i])
      && !isList.test(lines[i]) && !/^>\s?/.test(lines[i])) {
      buf.push(lines[i++]);
    }
    out.push(`<p>${mdInline(buf.join("\n"))}</p>`);
  }
  return out.join("\n");
}
