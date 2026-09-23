// 画面页：复用共享组件 assets/js/liveview.js（直播 + 缩放 + 交互 + 关遮挡），
// 这里只保留页面特有的工具：provider 选择、fps/quality、暂停、保存当前帧、状态行。
let provider = null;
let paused = false;
let lv = null;
// ?thread_id=xxx → 固定看这个会话的页面；不传则跟随"最近使用"的会话
let pinnedThread = new URLSearchParams(location.search).get("thread_id");

function showStatus(html) {
  const el = document.querySelector("#status");
  if (el) el.innerHTML = html;
}

function makeHeaderExtra() {
  // 页面特有控件放进组件的头部（暂停/保存帧仍在工具栏里，这里只放"状态")
  const span = document.createElement("span");
  span.className = "lv-extra muted";
  return span;
}

function initLive() {
  const pane = document.querySelector("#livePane");
  if (!pane || !window.LiveView) {
    showStatus("<span class='err'>组件加载失败（liveview.js）</span>");
    return;
  }
  lv = window.LiveView.create({
    pane: pane,
    getProvider: () => provider,
    getThreadId: () => pinnedThread || "",
    extraQuery: () => ({ fps: document.querySelector("#fps").value, quality: document.querySelector("#quality").value }),
    headerExtra: makeHeaderExtra(),
  });
  lv.setEnabled(true);
  refreshStatusLoop();
}

// 状态行（页面级的额外信息，组件头部已有 会话/观众/帧龄）
async function refreshStatus() {
  if (!provider) return;
  const q = pinnedThread ? `?thread_id=${encodeURIComponent(pinnedThread)}` : "";
  try {
    const res = await fetch(`/admin/${encodeURIComponent(provider)}/screen/state${q}`);
    const st = await res.json();
    const vp = st.viewport ? `${st.viewport.width}×${st.viewport.height}` : "—";
    showStatus(
      `${st.available ? "✅ 可用" : "⏸ 没有打开的页面"} · 视口 ${vp}` +
      (st.source ? ` · 帧来源 <b>${esc(st.source)}</b>` : "") +
      (st.busy ? ' · <b>⏳ 生成中（自动降帧）</b>' : "") +
      (st.error ? ` · <span class="err">${esc(st.error)}</span>` : "")
    );
    if (!st.available && !paused) {
      // 组件自己会显示"暂无打开的页面"提示；这里只更新状态行
      showStatus("⏸ 该 provider 当前没有打开的页面 —— 去 Playground 发一条消息后回来看");
    }
  } catch (e) {
    showStatus(`<span class="err">状态获取失败：${esc(e.message)}</span>`);
  }
}

let statusTimer = null;
function refreshStatusLoop() {
  if (statusTimer) clearInterval(statusTimer);
  refreshStatus();
  statusTimer = setInterval(refreshStatus, 2500);
}

async function loadProviders() {
  const data = await (await fetch("/admin/status")).json();
  const sel = document.querySelector("#provider");
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
    showStatus("<span class='err'>没有启用的 provider</span>");
    return;
  }
  sel.value = provider;
  initLive();
}

// ---- 事件 ----
document.querySelector("#provider").addEventListener("change", (e) => {
  provider = e.target.value;
  pinnedThread = null;               // 固定的 thread 可能不属于新 provider → 回到"跟随最近使用"
  lv && lv.refresh();
  refreshStatus();
});
for (const id of ["#fps", "#quality"]) {
  document.querySelector(id).addEventListener("change", () => lv && lv.refresh());
}
document.querySelector("#pause").onclick = () => {
  paused = !paused;
  document.querySelector("#pause").textContent = paused ? "继续" : "暂停";
  lv.setEnabled(!paused);
  refreshStatus();
};
document.querySelector("#save").onclick = async () => {
  if (!provider) return;
  try {
    const res = await fetch(`/admin/${encodeURIComponent(provider)}/screen.jpg?quality=${document.querySelector("#quality").value}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
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

loadProviders().catch((e) => showStatus(`<span class="err">初始化失败：${esc(e.message)}</span>`));
