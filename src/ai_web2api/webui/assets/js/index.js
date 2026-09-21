// 状态面板逻辑
let timer = null;

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
  renderServer(data.server);
  renderProviders(data.providers);
  renderThreads(data.threads);
}

function renderServer(s) {
  const keyed = [
    ["版本", s.version], ["运行时间", fmtUptime(s.uptime_seconds)],
    ["监听", `${s.host}:${s.port}`],
    ["headless", s.headless ? "是" : "否"],
    ["定时状态检测", s.status_check ? "开" : "关" + (s.status_check_headless ? "（headless）" : "")],
    ["thread TTL", `${s.thread_ttl}s`],
    ["max_threads", s.max_threads],
    ["thread 并行", s.thread_parallel ? "是" : "否"],
    ["thread 持久化", s.thread_persist ? "是" : "否"],
    ["API Key 鉴权", s.api_keys_configured ? "已配置" : "未启用"],
  ];
  $("#serverCard").innerHTML = `
    <div class="card">
      <h2>服务</h2>
      <dl class="kv">${keyed.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    </div>`;
  $("#serverLine").textContent = `v${s.version} · ${s.host}:${s.port} · 运行 ${fmtUptime(s.uptime_seconds)}`;
}

function renderProviders(providers) {
  const wrap = $("#providers");
  if (!providers.length) { wrap.innerHTML = `<div class="empty">未配置任何 provider</div>`; return; }
  wrap.innerHTML = providers.map((p) => {
    const cls = p.logged_in ? "logged-in" : "logged-out";
    const badge = p.logged_in
      ? `<span class="badge ok">✓ 已登录</span>`
      : `<span class="badge no">✗ 未登录</span>`;
    const chips = [
      ...p.models.map((m) => `<span class="chip">${esc(m)}</span>`),
      ...Object.entries(p.model_aliases || {}).map(
        ([alias, target]) => `<span class="chip aliased" title="别名">${esc(alias)} → ${esc(target)}</span>`
      ),
    ].join("");
    const stateFile = p.has_state_file
      ? `state.json ✓` : `state.json ✗（无持久化登录态）`;
    const shot = p.login_error_screenshot
      ? `<div class="p-shot"><div class="p-meta">⚠ 上次登录失败截图（${new Date((p.login_error_at || 0) * 1000).toLocaleString()}）：</div>` +
        `<a href="/admin/${encodeURIComponent(p.name)}/login/screenshot?t=${p.login_error_at || 0}" target="_blank" rel="noopener">` +
        `<img src="/admin/${encodeURIComponent(p.name)}/login/screenshot?t=${p.login_error_at || 0}" alt="登录失败截图" loading="lazy"></a></div>`
      : "";
    return `
    <div class="card provider ${cls}">
      <div class="p-head">
        <span class="name">${esc(p.name)}</span> ${badge}
        <span style="margin-left:auto;color:var(--muted);font-size:12px">driver: ${esc(p.driver)}</span>
      </div>
      <div class="p-meta">
        url: ${esc(p.url)}<br>
        登录模式: ${esc(p.login_mode)} · ${esc(stateFile)}<br>
        默认模型: ${esc(p.default_model || "—")} · response_timeout: ${p.response_timeout}s
      </div>
      <div class="chips">${chips}</div>
      ${shot}
      <div class="p-actions">
        <button class="btn" onclick="actLoginStatus('${p.name}')">刷新登录状态</button>
        <button class="btn" onclick="actAutoLogin('${p.name}')">自动登录</button>
        <button class="btn" onclick="actManualLogin('${p.name}')">手动登录</button>
        <button class="btn" onclick="actScreenshot('${p.name}')">抓取截图</button>
        <button class="btn danger" onclick="actLogout('${p.name}')">退出登录</button>
      </div>
    </div>`;
  }).join("");
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
  $("#threadCount").textContent = `${t.active} / ${t.max}`;
  const wrap = $("#threads");
  if (!t.list.length) { wrap.innerHTML = `<div class="empty">当前没有活跃 thread 会话</div>`; return; }
  const pct = Math.min(100, Math.round((t.active / Math.max(1, t.max)) * 100));
  wrap.innerHTML = `
    <div class="bar"><div style="width:${pct}%"></div></div>
    <table>
      <tr><th>thread_id</th><th>provider</th><th>model</th><th>idle</th><th>URL</th><th></th></tr>
      ${t.list.map((s) => `
        <tr>
          <td class="mono">${esc(s.thread_id)}</td>
          <td>${esc(s.provider)}</td>
          <td class="mono">${esc(s.model)}</td>
          <td>${s.idle_seconds}s</td>
          <td class="mono" title="${esc(s.page_url)}">${esc(s.url_id || "—")}</td>
          <td><button class="btn danger" onclick="killThread('${esc(s.thread_id)}')">销毁</button></td>
        </tr>`).join("")}
    </table>`;
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

$("#refreshBtn").onclick = refresh;
$("#autoRefresh").onchange = (e) => {
  clearInterval(timer);
  if (e.target.checked) timer = setInterval(refresh, 10000);
};
if ($("#autoRefresh").checked) timer = setInterval(refresh, 10000);
refresh();
