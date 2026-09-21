// 轻量 markdown 渲染（自包含，无 CDN 依赖；先 esc 防 XSS，再解析标记）
function mdInline(s) {
  s = esc(s);
  // 生成的 HTML 用占位符暂存：避免后续强调/下划线规则误伤 URL 与属性
  // （图片 URL 常含 `_`，旧实现会把 src 拆坏 → 图片加载失败被当“丢失”）
  const store = [];
  const stash = (html) => { store.push(html); return `\u0000${store.length - 1}\u0000`; };
  // 行内代码
  s = s.replace(/`([^`\n]+)`/g, (m, c) => stash(`<code>${c}</code>`));
  // 图片：![](url) 必须在链接之前（其内部含 [](url)）；包 <a> 便于点开大图
  s = s.replace(/!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g,
    (m, alt, u) => stash(`<a class="md-img" href="${u}" target="_blank" rel="noopener"><img src="${u}" alt="${alt}" loading="lazy" referrerpolicy="no-referrer"></a>`));
  // 链接：仅 http(s) 白名单
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    (m, t, u) => stash(`<a href="${u}" target="_blank" rel="noopener">${t}</a>`));
  // 裸 URL 自动链接（前面是行首/空白/(/> ；不会碰到已有 href/src/锚文本）
  s = s.replace(/(^|[\s(>])(https?:\/\/[^\s<)"']+)/g,
    (m, pre, u) => `${pre}${stash(`<a href="${u}" target="_blank" rel="noopener">${u}</a>`)}`);
  // 换行 → <br>
  s = s.replace(/\n/g, "<br>");
  // 粗体 / 斜体 / 删除线（此时只剩纯文本 + 占位符）
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  // 还原暂存的 HTML
  s = s.replace(/\u0000(\d+)\u0000/g, (m, i) => store[Number(i)]);
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
