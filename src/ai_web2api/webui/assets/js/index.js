// 状态面板逻辑
let timer = null;
let selectedProvider = null;
let lastData = null;

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 2600);
}

function fmtUptime(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return `${h}h ${pad(m)}m ${pad(s)}s`;
}

async function load() {
  const data = await api("/admin/status");
  lastData = data;
  renderKpis(data.server, data.threads || { active: 0, max: 0, list: [] });
  renderProviders(data.providers || []);
  renderThreads(data.threads || { active: 0, max: 0, list: [] });
}

function renderKpis(s, t) {
  const items = [
    ["运行时间", fmtUptime(s.uptime_seconds)],
    ["活跃会话", `${t.active} / ${t.max}`],
    ["监听", `${s.host}:${s.port}`],
    ["headless", s.headless ? "是" : "否"],
    ["thread 持久化", s.thread_persist ? "开" : "关"],
    ["定时检测", s.status_check ? "开" : "关"],
    ["API Key", s.api_keys_configured ? "已启用" : "未启用"],
    ["版本", s.version],
  ];
  $("#kpis").innerHTML = items
    .map(([k, v]) => `<div class="kpi"><div class="kpi-v">${esc(v)}</div><div class="kpi-k">${esc(k)}</div></div>`)
    .join("");
  $("#serverLine").textContent = `v${s.version} · ${s.host}:${s.port} · 运行 ${fmtUptime(s.uptime_seconds)}`;
}

function renderProviders(providers) {
  const tabs = $("#providerTabs");
  const detail = $("#providerDetail");
  if (!providers.length) {
    tabs.innerHTML = "";
    detail.innerHTML = `<div class="empty">未配置任何 provider</div>`;
    selectedProvider = null;
    return;
  }
  if (!providers.some((p) => p.name === selectedProvider)) selectedProvider = providers[0].name;
  tabs.innerHTML = providers
    .map(
      (p) => `<button class="tab ${p.name === selectedProvider ? "active" : ""}" data-name="${esc(p.name)}">
        <span class="dot ${p.logged_in ? "on" : "off"}"></span>${esc(p.name)}
        <span class="tab-sub">${esc(p.default_model || "")}</span>
      </button>`
    )
    .join("");
  renderProviderDetail(providers.find((p) => p.name === selectedProvider));
}

function renderProviderDetail(p) {
  const detail = $("#providerDetail");
  if (!p) { detail.innerHTML = ""; return; }
  const badge = p.logged_in
    ? `<span class="badge ok">✓ 已登录</span>`
    : `<span class="badge no">✗ 未登录</span>`;
  const chips =
    [
      ...p.models.map((m) => `<span class="chip">${esc(m)}</span>`),
      ...Object.entries(p.model_aliases || {}).map(
        ([alias, target]) => `<span class="chip aliased" title="别名">${esc(alias)} → ${esc(target)}</span>`
      ),
    ].join("") || `<span class="muted">无</span>`;
  const shot = p.login_error_screenshot
    ? `<div class="p-shot">
         <div class="p-shot-head">⚠ 上次登录失败截图（${new Date((p.login_error_at || 0) * 1000).toLocaleString()}）</div>
         <a href="/admin/${encodeURIComponent(p.name)}/login/screenshot?t=${p.login_error_at || 0}" target="_blank" rel="noopener">
           <img src="/admin/${encodeURIComponent(p.name)}/login/screenshot?t=${p.login_error_at || 0}" alt="登录失败截图" loading="lazy">
         </a>
       </div>`
    : "";
  detail.innerHTML = `
    <div class="pv-head">
      <span class="pv-name">${esc(p.name)}</span>
      ${badge}
      <span class="pv-driver">driver: ${esc(p.driver)}</span>
    </div>
    <dl class="kv">
      <dt>地址</dt><dd class="mono">${esc(p.url)}</dd>
      <dt>登录模式</dt><dd>${esc(p.login_mode)} · ${p.has_state_file ? "state.json ✓" : "state.json ✗（无持久化登录态）"}</dd>
      <dt>默认模型</dt><dd class="mono">${esc(p.default_model || "—")}</dd>
      <dt>响应超时</dt><dd>${p.response_timeout}s</dd>
    </dl>
    <div class="sec-title">模型 / 别名</div>
    <div class="chips">${chips}</div>
    ${shot}
    <div class="actions">
      <button class="btn" onclick="actLoginStatus('${p.name}')">刷新登录状态</button>
      <button class="btn primary" onclick="actAutoLogin('${p.name}')">自动登录</button>
      <button class="btn" onclick="actManualLogin('${p.name}')">手动登录</button>
      <button class="btn" onclick="actScreenshot('${p.name}')">抓取截图</button>
      <button class="btn danger" onclick="actLogout('${p.name}')">退出登录</button>
    </div>`;
}

async function actLoginStatus(name) {
  const btn = event.target; btn.disabled = true;
  try {
    const r = await api(`/admin/${name}/login/status`);
    toast(`${name}: ${r.logged_in ? "已登录 ✓" : "未登录 ✗"}`);
    await load();
  } catch (e) { toast(`失败: ${e.message}`); }
  finally { btn.disabled = false; }
}

async function actAutoLogin(name) {
  const btn = event.target; btn.disabled = true;
  try {
    const r = await api(`/admin/${name}/login/auto`, { method: "POST" });
    toast(r.already_logged_in ? `${name}: 已登录，无需重复登录` : `${name}: ${r.ok ? "自动登录成功 ✓" : "失败: " + (r.reason || "")}`);
    await load();
  } catch (e) { toast(`失败: ${e.message}`); }
  finally { btn.disabled = false; }
}

async function actManualLogin(name) {
  const btn = event.target; btn.disabled = true;
  try {
    const r = await api(`/admin/${name}/login/start`, { method: "POST" });
    toast(`已打开登录窗口（需 headless=false 才可见）。完成后点「刷新登录状态」。${r.hint || ""}`);
  } catch (e) { toast(`失败: ${e.message}`); }
  finally { btn.disabled = false; }
}

async function actScreenshot(name) {
  const btn = event.target; btn.disabled = true;
  try {
    const r = await api(`/admin/${name}/login/screenshot`, { method: "POST" });
    toast(`${name}: ${r.saved ? "已抓取截图" : "截图失败"}`);
    await load();
  } catch (e) { toast(`失败: ${e.message}`); }
  finally { btn.disabled = false; }
}

async function actLogout(name) {
  const btn = event.target; btn.disabled = true;
  try {
    await api(`/admin/${name}/login/logout`, { method: "POST" });
    toast(`${name}: 已退出登录`);
    await load();
  } catch (e) { toast(`失败: ${e.message}`); }
  finally { btn.disabled = false; }
}

function renderThreads(t) {
  $("#threadCount").textContent = `${t.active} / ${t.max} 活跃`;
  const wrap = $("#threads");
  const list = t.list || [];
  if (!list.length) {
    wrap.innerHTML = `<div class="empty">当前没有会话（发送消息后会出现在这里，可在 Playground 查看历史）</div>`;
    return;
  }
  const pct = Math.min(100, Math.round((t.active / Math.max(1, t.max)) * 100));
  wrap.innerHTML = `
    <div class="bar"><div style="width:${pct}%"></div></div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>thread_id</th><th>provider</th><th>model</th><th>状态</th><th>URL</th><th></th></tr></thead>
        <tbody>${list.map((s) => `
          <tr>
            <td class="mono">${esc(s.thread_id)}</td>
            <td>${esc(s.provider)}</td>
            <td class="mono">${esc(s.model || "")}</td>
            <td>${s.loaded === false
              ? `<span class="pill">存档</span>`
              : `<span class="pill live">活跃 · ${s.idle_seconds ?? 0}s</span>`}</td>
            <td class="mono" title="${esc(s.page_url || "")}">${esc(s.url_id || "—")}</td>
            <td><button class="btn danger sm" onclick="killThread('${esc(s.thread_id)}')">销毁</button></td>
          </tr>`).join("")}</tbody>
      </table>
    </div>`;
}

async function killThread(id) {
  try {
    await api(`/admin/threads/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast(`thread ${id} 已销毁`);
    await load();
  } catch (e) { toast(`失败: ${e.message}`); }
}

async function refresh() {
  try { await load(); }
  catch (e) {
    $("#serverLine").textContent = "无法连接服务: " + e.message;
    toast("状态加载失败: " + e.message);
  }
}

// ---- 事件绑定 ----
$("#refreshBtn").onclick = refresh;
$("#autoRefresh").onchange = (e) => {
  clearInterval(timer);
  if (e.target.checked) timer = setInterval(refresh, 10000);
};
$("#providerTabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  selectedProvider = btn.dataset.name;
  if (lastData) renderProviders(lastData.providers || []);
});
if ($("#autoRefresh").checked) timer = setInterval(refresh, 10000);
refresh();
