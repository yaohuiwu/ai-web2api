// 会话独立页：搜索（防抖）/ provider 过滤 / 分页加载更多 / 续用 / 销毁
const PAGE_DEFAULT = 20;
let items = [];
let total = 0;
let hasMore = false;
let loading = false;
let autoTimer = null;

const relTime = (ts) => {
  if (!ts) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s 前`;
  if (s < 3600) return `${Math.round(s / 60)}m 前`;
  if (s < 86400) return `${Math.round(s / 3600)}h 前`;
  return `${Math.round(s / 86400)}d 前`;
};
const absTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : "");

function currentQuery(offset) {
  const p = new URLSearchParams();
  const q = $("#q").value.trim();
  if (q) p.set("q", q);
  if ($("#provider").value) p.set("provider", $("#provider").value);
  p.set("limit", $("#limit").value || PAGE_DEFAULT);
  p.set("offset", String(offset));
  return p.toString();
}

async function loadMore(reset = false) {
  if (loading) return;
  loading = true;
  $("#moreBtn").disabled = true;
  const offset = reset ? 0 : items.length;
  if (reset) {
    items = [];
    $("#list").innerHTML = `<div class="empty">加载中…</div>`;
  }
  try {
    const data = await api(`/admin/threads?${currentQuery(offset)}`);
    total = data.total || 0;
    hasMore = !!data.has_more;
    items = reset ? data.threads || [] : items.concat(data.threads || []);
    render();
  } catch (e) {
    $("#list").innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
    $("#moreHint").textContent = "";
  } finally {
    loading = false;
    $("#moreBtn").disabled = false;
  }
}

function render() {
  const list = $("#list");
  const q = $("#q").value.trim();
  $("#summary").innerHTML = `已加载 <b>${items.length}</b> / 共 <b>${total}</b> 条${
    q ? ` · 搜索「${esc(q)}」` : ""
  }`;
  if (!items.length) {
    list.innerHTML = q || $("#provider").value
      ? `<div class="empty"><span class="big">🔍</span>没有匹配的会话</div>`
      : `<div class="empty"><span class="big">💬</span>还没有会话 —— 去 Playground 发一条消息</div>`;
  } else {
    list.innerHTML = items.map(card).join("");
    list.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = () => copyText(b.dataset.copy)));
    list.querySelectorAll("[data-kill]").forEach((b) => (b.onclick = () => killThread(b.dataset.kill)));
  }
  $("#moreBtn").hidden = !hasMore;
  $("#moreHint").textContent = hasMore ? `还有 ${total - items.length} 条` : items.length ? "已到底部" : "";
}

function card(t) {
  const id = t.thread_id;
  const title = t.first_message || id;
  const live = t.loaded !== false;
  const href = `/ui/playground.html?thread_id=${encodeURIComponent(id)}`;
  return `<article class="tcard">
    <div class="tc-main">
      <a class="tc-title" href="${href}" title="${esc(title)}">${esc(title)}</a>
      <div class="tc-meta">
        <span class="pill">${esc(t.provider || "—")}</span>
        <span class="mono">${esc(t.model || "")}</span>
        <span class="pill ${live ? "live" : ""}">${live ? "活跃" : "存档"}</span>
        <span title="${esc(absTime(t.updated_at))}">${relTime(t.updated_at)}</span>
        <span class="mono tc-id" title="${esc(id)}">${esc(id)}</span>
      </div>
    </div>
    <div class="tc-actions">
      <a class="btn sm" href="${href}" title="在 Playground 续用该会话">继续</a>
      <button class="btn sm" data-copy="${esc(id)}" title="复制 thread_id" aria-label="复制 thread_id">⧉</button>
      <button class="btn danger sm" data-kill="${esc(id)}" title="销毁（释放浏览器页面，保留历史）">销毁</button>
    </div>
  </article>`;
}

async function killThread(id) {
  try {
    await api(`/admin/threads/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast(`thread ${id} 已销毁`);
    await loadMore(true);
  } catch (e) {
    toast(`失败: ${e.message}`);
  }
}

function setAuto(on) {
  clearInterval(autoTimer);
  autoTimer = on ? setInterval(() => loadMore(true), 10000) : null;
}

async function loadProviders() {
  try {
    const data = await api("/admin/status");
    const sel = $("#provider");
    for (const p of data.providers || []) {
      const o = document.createElement("option");
      o.value = p.name;
      o.textContent = p.name;
      sel.appendChild(o);
    }
  } catch {
    /* 拿不到就只留"全部" */
  }
}

// ---- 事件绑定 ----
let debounce = null;
$("#q").addEventListener("input", () => {
  clearTimeout(debounce);
  debounce = setTimeout(() => loadMore(true), 300);
});
$("#provider").addEventListener("change", () => loadMore(true));
$("#limit").addEventListener("change", () => loadMore(true));
$("#refreshBtn").onclick = () => loadMore(true);
$("#moreBtn").onclick = () => loadMore(false);
$("#autoRefresh").addEventListener("change", (e) => setAuto(e.target.checked));

loadProviders().then(() => loadMore(true));
