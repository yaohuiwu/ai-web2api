// 状态面板逻辑
let timer = null;
let selectedProvider = null;
let lastData = null;

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
  renderQuickstart(data.server);
}

function renderKpis(s, t) {
  const onOff = (v) => (v ? "开" : "关");
  const items = [
    ["⏱", "运行时间", fmtUptime(s.uptime_seconds), ""],
    ["💬", "活跃会话", `${t.active} / ${t.max}`, t.active >= t.max ? "warn" : ""],
    ["🔌", "监听地址", `${s.host}:${s.port}`, ""],
    ["🖥", "headless", s.headless ? "是" : "否", s.headless ? "" : "muted"],
    ["💾", "thread 持久化", onOff(s.thread_persist), s.thread_persist ? "ok" : "muted"],
    ["🩺", "定时检测", onOff(s.status_check), s.status_check ? "ok" : "muted"],
    ["🔑", "API Key", s.api_keys_configured ? "已启用" : "未启用", s.api_keys_configured ? "ok" : "muted"],
    ["🏷", "版本", s.version, ""],
  ];
  $("#kpis").innerHTML = items
    .map(
      ([icon, k, v, tone]) => `<div class="kpi ${tone}" title="${esc(k)}">
        <div class="kpi-top"><span class="kpi-i">${icon}</span><span class="kpi-k">${esc(k)}</span></div>
        <div class="kpi-v">${esc(v)}</div>
      </div>`
    )
    .join("");
  $("#serverLine").textContent = `v${s.version} · ${s.host}:${s.port} · 运行 ${fmtUptime(s.uptime_seconds)}`;
  document.querySelectorAll("[data-version]").forEach((el) => (el.textContent = `v${s.version}`));
}

// 认证有效期的状态 → 状态点颜色（未登录/已过期 = 红，即将过期 = 黄）
function dotClass(p) {
  if (!p.logged_in) return "off";
  const st = p.auth_expiry && p.auth_expiry.state;
  if (st === "expired") return "off";
  if (st === "soon") return "warn";
  return "on";
}

// ---- 快速接入：按访问来源生成 curl / Python / Node 片段 ----
let qsActive = "curl";

function qsSnippets(s, model) {
  const origin = location.origin;
  const key = s.api_keys_configured ? "sk-你的key" : "unused";
  const auth = s.api_keys_configured ? " \\\n  -H 'Authorization: Bearer sk-你的key'" : "";
  return {
    curl:
      `curl -s ${origin}/v1/chat/completions \\\n` +
      `  -H 'Content-Type: application/json'${auth} \\\n` +
      `  -d '{"model":"${model}","messages":[{"role":"user","content":"你好"}]}'`,
    python:
      `# pip install openai\nfrom openai import OpenAI\n\n` +
      `client = OpenAI(base_url="${origin}/v1", api_key="${key}")\n` +
      `resp = client.chat.completions.create(\n    model="${model}",\n` +
      `    messages=[{"role": "user", "content": "你好"}],\n)\n` +
      `print(resp.choices[0].message.content)`,
    node:
      `// npm i openai\nimport OpenAI from "openai";\n\n` +
      `const client = new OpenAI({ baseURL: "${origin}/v1", apiKey: "${key}" });\n` +
      `const resp = await client.chat.completions.create({\n  model: "${model}",\n` +
      `  messages: [{ role: "user", content: "你好" }],\n});\n` +
      `console.log(resp.choices[0].message.content);`,
  };
}

function renderQuickstart(s) {
  const el = $("#quickstart");
  if (!el || !lastData) return;
  const list = lastData.providers || [];
  const cur = list.find((p) => p.name === selectedProvider) || list[0] || {};
  const model = cur.default_model || "deepseek-web";
  const snips = qsSnippets(s, model);
  el.innerHTML = `
    <div class="panel-head">
      <h2>快速接入</h2>
      <span class="muted">
        模型 <code>${esc(model)}</code> · ${s.api_keys_configured ? "需 API Key" : "未启用鉴权"} ·
        <a href="/docs" target="_blank" rel="noopener">/docs</a>
      </span>
    </div>
    <div class="qs">
      <div class="qs-tabs">
        ${Object.keys(snips).map((k) => `<button class="qs-tab ${k === qsActive ? "active" : ""}" data-qs="${k}">${k}</button>`).join("")}
      </div>
      <div class="qs-body">
        <button class="btn sm" id="qsCopy">复制</button>
        <pre class="qs-pre"><code></code></pre>
      </div>
    </div>`;
  const code = el.querySelector(".qs-pre code");
  const setTab = (k) => {
    qsActive = k;
    el.querySelectorAll(".qs-tab").forEach((b) => b.classList.toggle("active", b.dataset.qs === k));
    code.textContent = snips[k];
  };
  el.querySelectorAll(".qs-tab").forEach((b) => (b.onclick = () => setTab(b.dataset.qs)));
  el.querySelector("#qsCopy").onclick = () => copyText(snips[qsActive]);
  setTab(qsActive);
}

function renderProviders(providers) {
  const tabs = $("#providerTabs");
  const detail = $("#providerDetail");
  if (!providers.length) {
    tabs.innerHTML = "";
    detail.innerHTML = `<div class="empty"><span class="big">🔌</span>未配置任何 provider</div>`;
    selectedProvider = null;
    return;
  }
  if (!providers.some((p) => p.name === selectedProvider)) selectedProvider = providers[0].name;
  tabs.innerHTML = providers
    .map(
      (p) => `<button class="tab ${p.name === selectedProvider ? "active" : ""}" data-name="${esc(p.name)}">
        <span class="dot ${dotClass(p)}"></span>${esc(p.name)}
        <span class="tab-sub">${esc(p.default_model || "")}</span>
      </button>`
    )
    .join("");
  renderProviderDetail(providers.find((p) => p.name === selectedProvider));
}

function renderProviderDetail(p) {
  const detail = $("#providerDetail");
  if (!p) { detail.innerHTML = ""; return; }
  const badge = !p.logged_in
    ? `<span class="badge no">✗ 未登录</span>`
    : (p.auth_expiry && (p.auth_expiry.state === "soon" || p.auth_expiry.state === "expired"))
      ? `<span class="badge warn">✓ 已登录 · 认证${p.auth_expiry.state === "expired" ? "已过期" : "即将过期"}</span>`
      : `<span class="badge ok">✓ 已登录</span>`;
  const threadCount = ((lastData && lastData.threads && lastData.threads.list) || []).filter(
    (x) => x.provider === p.name
  ).length;
  const summary = `${p.models.length} 个模型 · ${Object.keys(p.model_aliases || {}).length} 个别名 · ${threadCount} 个活跃会话`;
  const chips =
    [
      ...p.models.map((m) => `<span class="chip">${esc(m)}</span>`),
      ...Object.entries(p.model_aliases || {}).map(
        ([alias, target]) => `<span class="chip aliased" title="别名">${esc(alias)} → ${esc(target)}</span>`
      ),
    ].join("") || `<span class="muted">无</span>`;
  const shot = p.login_error_screenshot && !p.logged_in
    ? `<div class="p-shot">
         <div class="p-shot-head">⚠ 上次登录失败截图（${new Date((p.login_error_at || 0) * 1000).toLocaleString()}）</div>
         <a href="/admin/${encodeURIComponent(p.name)}/login/screenshot?t=${p.login_error_at || 0}" target="_blank" rel="noopener">
           <img src="/admin/${encodeURIComponent(p.name)}/login/screenshot?t=${p.login_error_at || 0}" alt="登录失败截图" loading="lazy">
         </a>
       </div>`
    : "";
  const notLogged = !p.logged_in;
  const cmd = `python -m ai_web2api.cli login ${p.name}`;
  const notice = notLogged
    ? `<div class="login-cta">⚠ 未登录。在本机运行 <code>${esc(cmd)}</code>（登录完会自动导入），或在下方粘贴/选择 <code>state.json</code>。
         <button class="btn sm" onclick="copyText('${cmd}')">复制命令</button></div>`
    : "";
  // 认证有效期（后端 /admin/status 的 auth_expiry；可能缺失 → 降级显示）
  const ae = p.auth_expiry || {};
  const aeState = ae.state;
  const days = ae.days_left != null ? Math.max(0, Math.round(ae.days_left)) : null;
  const aeDate = ae.expires_at_iso ? ae.expires_at_iso.slice(0, 10) : "";
  let aeText = "—";
  if (aeState === "ok") aeText = `还剩 ${days} 天（${aeDate}）`;
  else if (aeState === "soon") aeText = `⚠ ${days} 天后过期（${aeDate}）`;
  else if (aeState === "expired") aeText = `已过期（${aeDate}）`;
  else if (aeState === "logged_out") aeText = "未登录";
  else if (aeState === "unknown") aeText = "无法判断（会话型 / 未配置关键 cookie）";
  // state.json 更新时间：过期/未知时可据此推算实际有效期
  const savedAt = ae.saved_at;
  const savedDate = ae.saved_at_iso ? ae.saved_at_iso.slice(0, 10) : "";
  const savedAgo =
    savedAt != null ? Math.max(0, Math.round((Date.now() / 1000 - savedAt) / 86400)) : null;
  const validityDays = ae.validity_days != null ? Math.round(ae.validity_days) : null;
  let savedText = "—";
  if (savedAt) savedText = `更新于 ${savedDate}（${savedAgo} 天前）`;
  // 首次登录时间（sidecar 记录；无则后端回退 state.json mtime 并标注来源）
  const loginAt = ae.login_at;
  const loginDate = ae.login_at_iso ? ae.login_at_iso.slice(0, 10) : "";
  const loginAgo =
    loginAt != null ? Math.max(0, Math.round((Date.now() / 1000 - loginAt) / 86400)) : null;
  let loginText = "—";
  if (loginAt) {
    loginText = `${loginDate}（${loginAgo} 天前）`;
    if (ae.login_at_source === "state_file") loginText += " · 按 state.json 推算";
    if (validityDays != null) loginText += ` · 推算有效期约 ${validityDays} 天`;
  }
  const aeClsExtra = (() => {
    // 有效期进度条：剩余 / 推算有效期（无推算值时不显示）
    if (aeState === "unknown" || days == null || !validityDays) return "";
    const pct = Math.max(0, Math.min(100, Math.round((days / validityDays) * 100)));
    return `<span class="auth-bar ${esc(aeState)}"><i style="width:${pct}%"></i></span>`;
  })();
  // 仅手动认证 provider 快过期/已过期时提醒（自动认证无需人工干预）
  const manualWarn = p.login_mode === "manual" && (aeState === "soon" || aeState === "expired");
  const expiryCard = manualWarn
    ? `<div class="login-cta ${aeState === "expired" ? "danger" : "warn"}">⚠ <b>${esc(p.name)} 认证${
        aeState === "expired" ? "已过期" : `约 ${days} 天后过期`
      }</b>。该 provider 为手动认证，请尽快更新：
         <code>${esc(cmd)}</code>
         <button class="btn sm" onclick="copyText('${cmd}')">复制命令</button>
         <button class="btn sm" onclick="actManualLogin('${p.name}')">导入登录态</button>
       </div>`
    : "";
  detail.innerHTML = `
    <div class="pv-head">
      <span class="pv-name">${esc(p.name)}</span>
      ${badge}
      <span class="pv-driver">driver: ${esc(p.driver)}</span>
    </div>
    ${notice}
    ${expiryCard}
    <div class="pv-summary">${esc(summary)}</div>
    <dl class="kv">
      <dt>地址</dt><dd class="mono">${esc(p.url)}</dd>
      <dt>登录模式</dt><dd>${esc(p.login_mode)} · ${p.has_state_file ? "state.json ✓" : "state.json ✗（无持久化登录态）"}</dd>
      <dt>认证有效期</dt><dd class="auth-${esc(aeState || "unknown")}">${aeText}${aeClsExtra}</dd>
      <dt>首次登录</dt><dd class="mono">${loginText}</dd>
      <dt>认证信息更新</dt><dd class="mono">${savedText}</dd>
      <dt>默认模型</dt><dd class="mono">${esc(p.default_model || "—")}</dd>
      <dt>响应超时</dt><dd>${p.response_timeout}s</dd>
    </dl>
    <div class="sec-title">模型 / 别名</div>
    <div class="chips">${chips}</div>
    ${shot}
    <div class="actions">
      <button class="btn" onclick="actLoginStatus('${p.name}')">刷新登录状态</button>
      <button class="btn primary" onclick="actAutoLogin('${p.name}')">自动登录</button>
      <button class="btn" onclick="actManualLogin('${p.name}')">导入登录态</button>
      <button class="btn" onclick="actScreenshot('${p.name}')">抓取截图</button>
      <button class="btn danger" onclick="actLogout('${p.name}')">退出登录</button>
    </div>
    <div class="manual-panel" id="manualPanel" ${notLogged ? "" : "hidden"}>
      <div class="mp-line">本机运行（登录完会自动导入）：<code id="manualCmd">${esc(cmd)}</code></div>
      <div class="mp-line muted">或粘贴 / 拖入 <code>state.json</code>：</div>
      <textarea id="statePaste" rows="5" spellcheck="false" placeholder='{"cookies":[...],"origins":[...]}'></textarea>
      <div class="mp-actions">
        <input type="file" id="stateFile" accept=".json,application/json" hidden>
        <button class="btn" onclick="document.getElementById('stateFile').click()">选择文件</button>
        <button class="btn primary" onclick="actImportState('${p.name}')">导入</button>
        <span class="muted" id="stateStatus"></span>
      </div>
    </div>`;
  initManualPanel();
}

function initManualPanel() {
  const panel = $("#manualPanel");
  if (!panel) return;
  const fileEl = $("#stateFile");
  const readFile = (f) => {
    if (!f) return;
    const r = new FileReader();
    r.onload = () => { $("#statePaste").value = r.result; $("#stateStatus").textContent = `已读入 ${f.name}`; };
    r.readAsText(f);
  };
  if (fileEl) fileEl.onchange = () => readFile(fileEl.files && fileEl.files[0]);
  panel.ondragover = (e) => e.preventDefault();
  panel.ondrop = (e) => {
    e.preventDefault();
    readFile(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]);
  };
}

async function actLoginStatus(name) {
  const btn = event.target; btn.disabled = true; btn.classList.add("loading");
  try {
    const r = await api(`/admin/${name}/login/status`);
    toast(`${name}: ${r.logged_in ? "已登录 ✓" : "未登录 ✗"}`);
    await load();
  } catch (e) { toast(`失败: ${e.message}`); }
  finally { btn.disabled = false; btn.classList.remove("loading"); }
}

async function actAutoLogin(name) {
  const btn = event.target; btn.disabled = true; btn.classList.add("loading");
  try {
    const r = await api(`/admin/${name}/login/auto`, { method: "POST" });
    toast(r.already_logged_in ? `${name}: 已登录，无需重复登录` : `${name}: ${r.ok ? "自动登录成功 ✓" : "失败: " + (r.reason || "")}`);
    await load();
  } catch (e) { toast(`失败: ${e.message}`); }
  finally { btn.disabled = false; btn.classList.remove("loading"); }
}

function actManualLogin(name) {
  const panel = $("#manualPanel");
  if (!panel) return;
  panel.hidden = !panel.hidden;
}

async function actImportState(name) {
  const status = $("#stateStatus");
  const txt = ($("#statePaste").value || "").trim();
  if (!txt) { status.textContent = "请先粘贴或选择 state.json"; return; }
  let state;
  try { state = JSON.parse(txt); } catch (e) { status.textContent = "JSON 解析失败: " + e.message; return; }
  status.textContent = "导入中…";
  try {
    const r = await api(`/admin/${name}/login/state`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(state),
    });
    status.textContent = r.logged_in ? "导入成功，已登录 ✓" : "已导入，但登录态未确认";
    toast(`${name}: 已导入登录态`);
    await load();
  } catch (e) {
    status.textContent = "导入失败: " + e.message;
  }
}

async function actScreenshot(name) {
  const btn = event.target; btn.disabled = true; btn.classList.add("loading");
  try {
    const r = await api(`/admin/${name}/login/screenshot`, { method: "POST" });
    toast(`${name}: ${r.saved ? "已抓取截图" : "截图失败"}`);
    await load();
  } catch (e) { toast(`失败: ${e.message}`); }
  finally { btn.disabled = false; btn.classList.remove("loading"); }
}

async function actLogout(name) {
  const btn = event.target; btn.disabled = true; btn.classList.add("loading");
  try {
    await api(`/admin/${name}/login/logout`, { method: "POST" });
    toast(`${name}: 已退出登录`);
    await load();
  } catch (e) { toast(`失败: ${e.message}`); }
  finally { btn.disabled = false; btn.classList.remove("loading"); }
}

function renderThreads(t) {
  $("#threadCount").textContent = `${t.active} / ${t.max} 活跃`;
  const wrap = $("#threads");
  const list = t.list || [];
  if (!list.length) {
    wrap.innerHTML = `<div class="empty"><span class="big">💬</span>当前没有会话 —— 在 Playground 发一条消息就会出现在这里</div>`;
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
            <td class="mono">
              <span title="${esc(s.thread_id)}">${esc(s.thread_id)}</span>
              <button class="btn sm copy" data-copy="${esc(s.thread_id)}" title="复制 thread_id" aria-label="复制 thread_id">⧉</button>
            </td>
            <td><span class="pill">${esc(s.provider)}</span></td>
            <td class="mono">${esc(s.model || "")}</td>
            <td>${s.loaded === false
              ? `<span class="pill">存档</span>`
              : `<span class="pill live">活跃 · ${s.idle_seconds ?? 0}s</span>`}</td>
            <td class="mono" title="${esc(s.page_url || "")}">${esc(s.url_id || "—")}</td>
            <td class="row-actions">
              <a class="btn sm" href="/ui/playground.html?thread_id=${encodeURIComponent(s.thread_id)}" title="在 Playground 继续该会话">继续</a>
              <button class="btn danger sm" onclick="killThread('${esc(s.thread_id)}')">销毁</button>
            </td>
          </tr>`).join("")}</tbody>
      </table>
    </div>`;
  wrap.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = () => copyText(b.dataset.copy)));
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
