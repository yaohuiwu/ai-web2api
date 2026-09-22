// 实时画面（只读直播）：MJPEG <img> + 状态轮询。设计见 docs/LIVE_VIEW.md（P1）
let provider = null;
let paused = false;
let stateTimer = null;
// ?thread_id=xxx → 固定看这个会话的页面；不传则跟随"最近使用"的会话
let pinnedThread = new URLSearchParams(location.search).get("thread_id");

function streamUrl(p) {
  const q = new URLSearchParams({ fps: $("#fps").value, quality: $("#quality").value });
  if (pinnedThread) q.set("thread_id", pinnedThread);
  return `/admin/${encodeURIComponent(p)}/stream.mjpg?${q}`;
}

function stateUrl(p) {
  const q = pinnedThread ? `?thread_id=${encodeURIComponent(pinnedThread)}` : "";
  return `/admin/${encodeURIComponent(p)}/screen/state${q}`;
}

function showPlaceholder(text, spinner = false) {
  const ph = $("#placeholder");
  ph.hidden = false;
  ph.textContent = text;
  if (spinner) ph.textContent = "正在连接直播流… " + text;
}

function start() {
  if (!provider) return;
  paused = false;
  $("#pause").textContent = "暂停";
  $("#placeholder").hidden = true;
  const img = $("#live");
  img.hidden = false;
  img.src = `${streamUrl(provider)}&t=${Date.now()}`;
  refreshState();
}

function stop(reason) {
  paused = true;
  $("#live").src = "";
  $("#pause").textContent = "继续";
  showPlaceholder(reason || "已暂停");
}

async function refreshState() {
  try {
    const st = await api(stateUrl(provider));
    const vp = st.viewport ? `${st.viewport.width}×${st.viewport.height}` : "—";
    const from = st.shown_thread_id
      ? `会话 <b class="mono">${esc(st.shown_thread_id)}</b>`
      : "非会话页（登录页等）";
    $("#status").innerHTML =
      `${from} · 视口 ${vp} · 观众 ${st.viewers} · <span class="mono">${esc(st.page_url || "—")}</span>` +
      (st.busy ? ' · <b>⏳ 生成中（自动降帧 1fps）</b>' : "") +
      (st.error ? ` · <span class="err">${esc(st.error)}</span>` : "");
    if (!st.available && !paused) {
      stop("该 provider 当前没有打开的页面 —— 先去 Playground 发一条消息，或点「开始登录」后再回来看");
    }
  } catch (e) {
    $("#status").innerHTML = `<span class="err">状态获取失败：${esc(e.message)}</span>`;
  }
}

async function loadProviders() {
  const data = await api("/admin/status");
  const sel = $("#provider");
  sel.innerHTML = "";
  const list = data.providers || [];
  for (const p of list) {
    const o = document.createElement("option");
    o.value = p.name;
    o.textContent = p.name + (p.logged_in ? "" : "（未登录）");
    sel.appendChild(o);
  }
  const names = list.map((p) => p.name);
  const want = new URLSearchParams(location.search).get("provider");
  provider = want && names.includes(want) ? want : names[0];
  if (!provider) {
    showPlaceholder("没有启用的 provider");
    return;
  }
  sel.value = provider;
  start();
  clearInterval(stateTimer);
  stateTimer = setInterval(refreshState, 2000);
}

// ---- 事件 ----
$("#live").addEventListener("error", () => {
  if (!paused) stop("无法连接直播流（可能：没有打开的页面 / live_view=false / 服务已停止）");
});
$("#live").addEventListener("load", () => {
  $("#placeholder").hidden = true;
});
$("#provider").addEventListener("change", (e) => {
  provider = e.target.value;
  pinnedThread = null;   // 固定的 thread 可能不属于新 provider → 回到"跟随最近使用"
  start();
});
for (const id of ["#fps", "#quality"]) {
  $(id).addEventListener("change", () => {
    if (!paused) start();
  });
}
$("#pause").onclick = () => (paused ? start() : stop("已暂停（服务端会在宽限期后停止采集）"));
$("#save").onclick = async () => {
  if (!provider) return;
  try {
    const r = await fetch(`/admin/${encodeURIComponent(provider)}/screen.jpg?quality=${$("#quality").value}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${provider}-${Date.now()}.jpg`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast("已保存当前帧");
  } catch (e) {
    toast(`截图失败: ${e.message}`);
  }
};

loadProviders().catch((e) => showPlaceholder(`初始化失败：${e.message}`));
