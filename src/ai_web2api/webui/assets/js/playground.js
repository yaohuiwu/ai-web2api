// Playground 逻辑
let messages = [];      // 完整 OpenAI messages（含 assistant.tool_calls / role=tool）
let rawLog = [];        // [{req, res|error}]
let streaming = false;
let pendingAttachments = [];   // 待发送附件 [{name, mime, dataUrl}]
const MAX_ATT_MB = 20;

const apiKeyEl = $("#apiKey");
apiKeyEl.value = localStorage.getItem("aiw2api_api_key") || "";
apiKeyEl.addEventListener("change", () =>
  localStorage.setItem("aiw2api_api_key", apiKeyEl.value));

// ---------- Function Calling：内置 mock 工具 ----------
// 网页端只负责“模型说要用哪个工具”；工具的真正执行在客户端。
// Playground 用这些 mock 模拟本地执行，从而端到端跑通 FC 闭环。
const MOCK_TOOLS = {
  get_weather: (a) =>
    JSON.stringify({ city: a.city || "unknown", temp_c: 22, condition: "晴", humidity: "55%" }),
  get_time: () => JSON.stringify({ time: new Date().toISOString(), timezone: "UTC" }),
};

function runMockTool(name, argsJson) {
  let args = {};
  try { args = JSON.parse(argsJson || "{}"); } catch {}
  const fn = MOCK_TOOLS[name];
  const out = fn ? fn(args) : JSON.stringify({ error: `没有 ${name} 的 mock 实现` });
  return typeof out === "string" ? out : JSON.stringify(out);
}

// 相对时间（会话列表用）
function relTime(ts) {
  if (!ts) return "";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s 前`;
  if (s < 3600) return `${Math.round(s / 60)}m 前`;
  if (s < 86400) return `${Math.round(s / 3600)}h 前`;
  return `${Math.round(s / 86400)}d 前`;
}

function footTiming(div, ttfbMs, totalMs) {
  const el = div && div.querySelector(".mf-timing");
  if (!el) return;
  const f = (ms) => (ms == null ? "—" : `${(ms / 1000).toFixed(1)}s`);
  el.textContent = `首字 ${f(ttfbMs)} · 总 ${f(totalMs)}`;
}

function wireMsgActions(div) {
  div.querySelectorAll("[data-act]").forEach((btn) => {
    btn.onclick = () => {
      const isJson = btn.dataset.act === "copy-json";
      const payload = div._payload ?? { content: div._text || "" };
      copyText(isJson ? JSON.stringify(payload, null, 2) : div._text || "");
      const old = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(() => (btn.textContent = old), 1200);
    };
  });
}

// 交互组件（iframe widget）面板：默认沙箱 iframe 可交互，可切截图（见 docs/WIDGET_CAPTURE.md）
function widgetsHtml(widgets) {
  return (widgets || [])
    .map((w, i) => {
      const prov = encodeURIComponent(w.provider || "");
      const base = `/admin/${prov}/widgets/${encodeURIComponent(w.id)}`;
      const interactive = w.html
        ? `<iframe class="widget-frame" src="${base}.html" sandbox="allow-scripts" loading="lazy"
                   title="交互组件 ${esc(w.id)}"></iframe>`
        : "";
      const shot = w.png
        ? `<img class="widget-shot" src="${base}.png" alt="组件截图 ${esc(w.id)}" hidden>`
        : "";
      const toggle = w.html && w.png
        ? `<button class="btn sm" data-widget-toggle="${i}">看截图</button>`
        : "";
      return `<div class="widget-box" data-widget="${i}">
        <div class="widget-bar">
          <span class="muted">🧩 交互组件</span>
          ${toggle}
          <a class="btn sm" href="${base}.html" target="_blank" rel="noopener">新窗口打开</a>
          <button class="btn sm" data-widget-html="${i}">复制 HTML</button>
        </div>
        ${interactive}${shot}
      </div>`;
    })
    .join("");
}

function wireWidgets(root, widgets) {
  (widgets || []).forEach((w, i) => {
    const box = root.querySelector(`.widget-box[data-widget="${i}"]`);
    if (!box) return;
    const frame = box.querySelector(".widget-frame");
    const shot = box.querySelector(".widget-shot");
    const toggle = box.querySelector(`[data-widget-toggle="${i}"]`);
    if (toggle && frame && shot) {
      toggle.onclick = () => {
        const showingShot = !shot.hidden;
        shot.hidden = showingShot;
        frame.hidden = !showingShot;
        toggle.textContent = showingShot ? "看截图" : "看交互";
      };
    }
    const copyBtn = box.querySelector(`[data-widget-html="${i}"]`);
    if (copyBtn) {
      copyBtn.onclick = async () => {
        try {
          const prov = encodeURIComponent(w.provider || "");
          const r = await fetch(`/admin/${prov}/widgets/${encodeURIComponent(w.id)}.html`);
          copyText(await r.text());
        } catch (e) {
          toast(`复制失败: ${e.message}`);
        }
      };
    }
  });
}

function addMsg(role, content, opts = {}) {
  const chat = $("#chat");
  const div = document.createElement("div");
  div.className = "msg " + (opts.error ? "error" : role);
  let inner = `<div class="bubble">`;
  if (opts.thinking && opts.thinking.trim()) {
    inner += `<details class="thinking"><summary>💭 思考过程</summary>${md(opts.thinking)}</details>`;
  }
  inner += md(content);
  if (opts.attachments && opts.attachments.length) {
    inner +=
      `<div class="att-thumbs">` +
      opts.attachments
        .map((a) => `<a class="att-thumb" href="${a.dataUrl}" target="_blank" rel="noopener">${attMedia(a)}</a>`)
        .join("") +
      `</div>`;
  }
  if (opts.widgets && opts.widgets.length) {
    inner += `<div class="widgets">${widgetsHtml(opts.widgets)}</div>`;
  }
  // 底部信息行：角色 / 时间 / 耗时 / 动作
  if (role !== "meta") {
    const roleLabel = { user: "你", assistant: "助手", error: "错误" }[role] || role;
    const time = new Date().toLocaleTimeString([], { hour12: false });
    inner += `<div class="msg-foot"><span class="mf-role">${esc(roleLabel)}</span><span>${time}</span>`;
    if (role === "assistant") {
      inner += `<span class="mf-timing"></span>`;
      inner += `<button class="btn sm" data-act="copy">复制</button>`;
      inner += `<button class="btn sm" data-act="copy-json">复制 JSON</button>`;
    }
    inner += `</div>`;
  }
  inner += `</div>`;
  div.innerHTML = inner;
  div._text = content;
  div._payload = opts.payload || null;
  wireMsgActions(div);
  wireWidgets(div, opts.widgets);
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

// 等待响应中的占位提示（动点 + 已等秒数），避免看起来像卡死。
// 返回 stop()：幂等，首个 chunk 到达 / 请求结束 / 报错时调用。
function showPending(div) {
  const bubble = div && div.querySelector(".bubble");
  if (!bubble) return () => {};
  const p = document.createElement("div");
  p.className = "pending";
  p.innerHTML = '<span class="dots"><i></i><i></i><i></i></span><span class="ptext">等待响应…</span>';
  const foot = bubble.querySelector(".msg-foot");
  if (foot) bubble.insertBefore(p, foot);
  else bubble.appendChild(p);
  const t0 = Date.now();
  const timer = setInterval(() => {
    const el = p.querySelector(".ptext");
    if (el) el.textContent = `等待响应… ${Math.round((Date.now() - t0) / 1000)}s`;
  }, 1000);
  let stopped = false;
  return () => {
    if (stopped) return;
    stopped = true;
    clearInterval(timer);
    p.remove();
  };
}

function addToolCallMsg(calls) {
  const chat = $("#chat");
  for (const tc of calls) {
    const div = document.createElement("div");
    div.className = "msg tool-call";
    div.innerHTML =
      `<div class="bubble"><div class="tool-title">🔧 工具调用 · <code>${esc(tc.function.name)}</code></div>` +
      `<pre>${esc(tc.function.arguments || "{}")}</pre></div>`;
    chat.appendChild(div);
    chat.scrollTop = chat.scrollHeight;
  }
}

function addToolResultMsg(name, result) {
  const chat = $("#chat");
  const div = document.createElement("div");
  div.className = "msg tool-result";
  div.innerHTML =
    `<div class="bubble"><div class="tool-title">↩︎ 工具结果 · <code>${esc(name)}</code></div>` +
    `<pre>${esc(result)}</pre></div>`;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

// ---------- 附件（图片/视频）----------
function attMedia(a) {
  return (a.mime || "").startsWith("video/")
    ? `<video src="${a.dataUrl}" muted playsinline></video>`
    : `<img src="${a.dataUrl}" alt="${esc(a.name)}">`;
}

function addAttachment(file) {
  if (!file) return;
  if (file.size > MAX_ATT_MB * 1024 * 1024) {
    $("#hint").textContent = `附件 ${file.name} 超过 ${MAX_ATT_MB}MB，已跳过`;
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    pendingAttachments.push({
      name: file.name,
      mime: file.type || "application/octet-stream",
      dataUrl: reader.result,
    });
    renderAttachments();
  };
  reader.readAsDataURL(file);
}

function renderAttachments() {
  const box = $("#attachments");
  if (!pendingAttachments.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML = pendingAttachments
    .map(
      (a, i) =>
        `<div class="att">${attMedia(a)}<span class="att-name">${esc(a.name)}</span>` +
        `<button class="att-del" data-i="${i}" title="移除">×</button></div>`
    )
    .join("");
  box.querySelectorAll(".att-del").forEach((b) => {
    b.onclick = () => {
      pendingAttachments.splice(Number(b.dataset.i), 1);
      renderAttachments();
    };
  });
}

function clearAttachments() {
  pendingAttachments = [];
  renderAttachments();
}

function addMeta(text) {
  const chat = $("#chat");
  const div = document.createElement("div");
  div.className = "msg meta";
  div.innerHTML = `<div class="bubble">${esc(text)}</div>`;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

async function loadModels() {
  try {
    const r = await fetch("/v1/models");
    const data = await r.json();
    const sel = $("#model");
    sel.innerHTML = "";
    for (const m of data.data || []) {
      const opt = document.createElement("option");
      opt.value = m.id;
      opt.textContent = m.id + (m.owned_by ? `  (${m.owned_by})` : "");
      sel.appendChild(opt);
    }
    const stored = localStorage.getItem("aiw2api_model");
    if (stored && [...sel.options].some((o) => o.value === stored)) sel.value = stored;
    if (!sel.value && sel.options.length) sel.value = sel.options[0].value;
  } catch (e) {
    $("#model").innerHTML = `<option>模型列表加载失败: ${esc(e.message)}</option>`;
  }
}

function currentTools() {
  if (!$("#useTools").checked) return null;
  try {
    const arr = JSON.parse($("#toolsJson").value || "[]");
    return Array.isArray(arr) && arr.length ? arr : null;
  } catch {
    return null;
  }
}

function buildBody(attachParts) {
  // 附件只附着在“本轮”最后一条 user 消息上；历史保持文本，避免每轮重复上传
  const msgs = messages.map((m, i) => {
    if (attachParts && attachParts.length && i === messages.length - 1 && m.role === "user") {
      return {
        role: "user",
        content: [{ type: "text", text: String(m.content || "") }, ...attachParts],
      };
    }
    return m;
  });
  const body = {
    model: $("#model").value,
    messages: msgs,
    stream: $("#stream").checked,
    deep_think: $("#deepThink").checked,
    search: $("#search").checked,
  };
  const tid = $("#threadId").value.trim();
  if (tid) body.thread_id = tid;
  const mode = $("#mode").value;
  if (mode) body.mode = mode;
  const tools = currentTools();
  if (tools) {
    body.tools = tools;
    const tc = $("#toolChoice").value;
    if (tc) body.tool_choice = tc;
  }
  return body;
}

function headers() {
  const h = { "Content-Type": "application/json" };
  const key = apiKeyEl.value.trim();
  if (key) h["Authorization"] = "Bearer " + key;
  return h;
}

function errMsg(body) {
  if (body && body.error && body.error.message) return body.error.message;
  if (body && body.detail) return JSON.stringify(body.detail);
  return "HTTP 错误";
}

// 服务端为无 thread_id 的请求自动分配会话：写回输入框，后续消息复用同一会话
function rememberThreadId(tid) {
  if (!tid) return;
  const el = $("#threadId");
  if (!el || el.value.trim()) return;   // 元素缺失 / 用户已显式填了 thread_id → 不覆盖
  el.value = tid;
  el.dispatchEvent(new Event("input"));
  refreshThreads();
}

// ---------- 发送：非流式 / 流式 ----------

async function sendNonStream(body) {
  const t0 = Date.now();
  // 先把"等待中"气泡摆上（非流式要等整包，不显示就会以为卡死）
  const pendingDiv = addMsg("assistant", "");
  const stopPending = showPending(pendingDiv);
  let data;
  try {
    const res = await fetch("/v1/chat/completions", {
      method: "POST", headers: headers(), body: JSON.stringify(body),
    });
    data = await res.json();
    if (!res.ok) throw new Error(errMsg(data));
  } finally {
    stopPending();
    pendingDiv.remove();   // 真实回复紧接着 addMsg 追加
  }
  rawLog.push({ req: body, res: data });
  const msg = (data.choices && data.choices[0] && data.choices[0].message) || {};
  if (msg.tool_calls && msg.tool_calls.length) {
    addToolCallMsg(msg.tool_calls);
  } else {
    const content = msg.content || "";
    const th = msg.reasoning_content;
    let div;
    if (th && !content) {
      div = addMsg("assistant", "（思考过程已完整输出，但正文未生成——可能因思考过长被结束判定截断，请重试）", { thinking: th, payload: msg });
    } else {
      div = addMsg("assistant", content || "（空回复）", {
        thinking: th, payload: msg, widgets: msg.widgets || null,
      });
    }
    footTiming(div, null, Date.now() - t0);
  }
  if (data.thread_id) {
    addMeta(`thread_id: ${data.thread_id}`);
    rememberThreadId(data.thread_id);
  }
  return { content: msg.content || "", thinking: msg.reasoning_content || "", toolCalls: msg.tool_calls || [] };
}

async function sendStream(body) {
  const res = await fetch("/v1/chat/completions", {
    method: "POST", headers: headers(), body: JSON.stringify(body),
  });
  if (!res.ok) {
    let data = null;
    try { data = await res.json(); } catch {}
    throw new Error(errMsg(data));
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let fullContent = "";
  let fullThinking = "";
  let thinkBox = null;
  let toolCalls = [];
  let gotWidgets = null;
  const bubbleDiv = addMsg("assistant", "");
  const bubble = bubbleDiv.querySelector(".bubble");
  const contentBox = document.createElement("div");
  contentBox.className = "content";
  bubble.appendChild(contentBox);
  const stopPending = showPending(bubbleDiv);
  const t0 = Date.now();
  let ttfb = null;
  let gotThreadId = false;

  const ensureThinkBox = () => {
    if (thinkBox) return thinkBox;
    thinkBox = document.createElement("details");
    thinkBox.className = "thinking";
    thinkBox.open = true;
    const s = document.createElement("summary");
    s.textContent = "💭 思考过程";
    thinkBox.appendChild(s);
    bubble.insertBefore(thinkBox, contentBox);
    return thinkBox;
  };

  const flushChunk = (delta) => {
    if (!delta) return;
    // 有任何实际内容（思考/正文/工具）→ 立即撤掉"等待中"指示
    if (delta.reasoning_content || delta.content || delta.tool_calls) {
      if (ttfb === null) ttfb = Date.now() - t0;
      stopPending();
    }
    if (delta.reasoning_content) {
      fullThinking += delta.reasoning_content;
      ensureThinkBox().appendChild(document.createTextNode(delta.reasoning_content));
    }
    if (delta.content) {
      fullContent += delta.content;
      contentBox.textContent = fullContent;
      $("#chat").scrollTop = $("#chat").scrollHeight;
    }
    if (delta.tool_calls) {
      for (const tcd of delta.tool_calls) {
        const i = tcd.index || 0;
        const tc = toolCalls[i] || (toolCalls[i] = { id: "", type: "function", function: { name: "", arguments: "" } });
        if (tcd.id) tc.id = tcd.id;
        if (tcd.function) {
          if (tcd.function.name) tc.function.name += tcd.function.name;
          if (tcd.function.arguments) tc.function.arguments += tcd.function.arguments;
        }
      }
    }
  };

  try {
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop();
    for (const line of lines) {
      const t = line.trim();
      if (!t.startsWith("data:")) continue;
      const payload = t.slice(5).trim();
      if (payload === "[DONE]") continue;
      let obj;
      try { obj = JSON.parse(payload); } catch { continue; }
      if (obj.error) throw new Error(obj.error.message || "流错误");
      if (obj.widgets && obj.widgets.length) gotWidgets = obj.widgets;
      const delta = obj.choices && obj.choices[0] && obj.choices[0].delta;
      if (delta) flushChunk(delta);
      if (obj.thread_id && !gotThreadId) {
        gotThreadId = true;
        addMeta(`thread_id: ${obj.thread_id}`);
        rememberThreadId(obj.thread_id);
      }
    }
  }
  } finally {
    stopPending();   // 正常结束 / 报错都要撤掉"等待中"指示
  }
  toolCalls = toolCalls.filter(Boolean);

  if (thinkBox && !fullThinking) {
    thinkBox.remove();
  } else if (thinkBox) {
    const open = thinkBox.open;
    thinkBox.innerHTML = "<summary>💭 思考过程</summary>" + md(fullThinking);
    thinkBox.open = open;
  }
  rawLog.push({ req: body, res: { content: fullContent, thinking: fullThinking, tool_calls: toolCalls } });

  if (toolCalls.length) {
    bubbleDiv.remove();            // 工具轮没有正文，去掉空气泡
    addToolCallMsg(toolCalls);
  } else if (!fullContent && fullThinking) {
    contentBox.innerHTML = "<p>（思考过程已完整输出，但正文未生成——可能因思考过长被结束判定截断，请重试）</p>";
  } else if (!fullContent && !fullThinking) {
    contentBox.textContent = "（空回复）";
  } else {
    contentBox.innerHTML = md(fullContent);
    if (/^\s*\[(会话错误|错误)\]/.test(fullContent)) {
      bubble.classList.add("error");
      contentBox.style.color = "var(--red-strong)";
    }
  }
  bubbleDiv._text = fullContent;
  bubbleDiv._payload = { content: fullContent, reasoning_content: fullThinking, tool_calls: toolCalls };
  if (gotWidgets) {
    const box = document.createElement("div");
    box.className = "widgets";
    box.innerHTML = widgetsHtml(gotWidgets);
    bubble.appendChild(box);
    wireWidgets(bubbleDiv, gotWidgets);
  }
  footTiming(bubbleDiv, ttfb, Date.now() - t0);
  return { content: fullContent, thinking: fullThinking, toolCalls, elapsed_ms: Date.now() - t0 };
}

// 一轮请求 + （可选）工具自动执行闭环
async function dispatchLoop(firstAttachments = null) {
  $("#send").disabled = true;
  streaming = true;
  $("#hint").textContent = "⏳ 生成中…（网页端较慢，ChatGPT 可能数十秒无输出，请稍候）";
  try {
    for (let round = 0; round < 6; round++) {
      const body = buildBody(round === 0 ? firstAttachments : null);
      const result = body.stream ? await sendStream(body) : await sendNonStream(body);
      if (result.toolCalls && result.toolCalls.length) {
        messages.push({ role: "assistant", content: null, tool_calls: result.toolCalls });
        if (!$("#autoExec").checked) break;   // 不自动执行 → 等用户手动处理
        for (const tc of result.toolCalls) {
          const out = runMockTool(tc.function.name, tc.function.arguments);
          messages.push({ role: "tool", tool_call_id: tc.id, content: out });
          addToolResultMsg(tc.function.name, out);
        }
        continue;   // 带上工具结果再请求 → 期望得到最终回答
      }
      messages.push({ role: "assistant", content: result.content || "" });
      break;
    }
    $("#hint").textContent = "";
  } catch (e) {
    addMsg("error", "请求失败: " + e.message);
    rawLog.push({ error: e.message });
    $("#hint").textContent = "提示：503 = 未登录；429 = 队列满/thread 超限；409 = thread 冲突或失效";
  } finally {
    streaming = false;
    $("#send").disabled = false;
    $("#input").focus();
    refreshThreads();
  }
}

async function send() {
  const text = $("#input").value.trim();
  if ((!text && !pendingAttachments.length) || streaming) return;
  const atts = pendingAttachments.slice();
  const parts = atts.map((a) => ({ type: "image_url", image_url: { url: a.dataUrl } }));
  messages.push({ role: "user", content: text });   // 历史存文本，附件只发本轮
  addMsg("user", text, { attachments: atts, payload: { role: "user", content: text } });
  localStorage.setItem("aiw2api_model", $("#model").value);
  const tid = $("#threadId").value.trim();
  if (tid) addMeta(`thread_id: ${tid}（绑定会话，续用只发最后一条）`);
  $("#input").value = "";
  clearAttachments();
  await dispatchLoop(parts);
}

$("#send").onclick = send;
$("#clear").onclick = () => {
  messages = [];
  $("#chat").innerHTML = `<div class="msg meta"><div class="bubble">已清空。${$("#threadId").value.trim() ? "（thread_id 仍保留，可继续原会话）" : ""}</div></div>`;
};
// IME（输入法）组词保护：中文/日文等组词时按回车是「选词上屏」，不应发送。
const inputEl = $("#input");
let composing = false;
let composeEndedAt = 0;
inputEl.addEventListener("compositionstart", () => { composing = true; });
inputEl.addEventListener("compositionend", () => {
  composing = false;
  composeEndedAt = Date.now();
});
inputEl.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.shiftKey) return;
  if (e.isComposing || e.keyCode === 229 || composing) return;
  if (Date.now() - composeEndedAt < 80) return;
  e.preventDefault();
  send();
});
$("#threadId").addEventListener("change", () => { messages = []; });
$("#threadId").addEventListener("input", (e) => {
  const tid = e.target.value.trim();
  $("#hint").textContent = tid
    ? "已启用会话绑定：首次请求创建会话并注入全部历史，后续 resume 只发最后一条；深度思考/智能搜索可随时切换"
    : "";
});
$("#mode").addEventListener("change", (e) => {
  const v = e.target.value;
  if (v === "fast") {
    $("#deepThink").checked = false;
    $("#search").checked = false;
  } else if (v === "expert") {
    $("#deepThink").checked = true;
    $("#search").checked = true;
  }
  $("#hint").textContent = v
    ? "mode 预设已套用到开关（fast = 思考关 + 搜索关，expert = 都开）；每次请求都可切换"
    : "";
});

// ---- 工具面板 ----
function setupToolsPanel() {
  const toolsJsonEl = $("#toolsJson");
  const sample = [
    {
      type: "function",
      function: {
        name: "get_weather",
        description: "查询某城市当前天气",
        parameters: { type: "object", properties: { city: { type: "string" } }, required: ["city"] },
      },
    },
    {
      type: "function",
      function: {
        name: "get_time",
        description: "获取当前时间",
        parameters: { type: "object", properties: {} },
      },
    },
  ];
  toolsJsonEl.value = localStorage.getItem("aiw2api_tools") || JSON.stringify(sample, null, 2);
  toolsJsonEl.addEventListener("change", () => localStorage.setItem("aiw2api_tools", toolsJsonEl.value));
  $("#useTools").addEventListener("change", () => {
    $("#toolPanel").hidden = !$("#useTools").checked;
  });
}

// ---- 左侧会话列表（点击切换 thread，手动刷新）----
function refreshThreads() {
  const cur = $("#threadId").value.trim();
  const search = $("#threadSearch");
  const q = (search ? search.value.trim() : "");
  // 服务端搜索（此前是在已加载列表里本地过滤 → 超出上限就搜不到）
  const params = new URLSearchParams({ limit: "50" });
  if (q) params.set("q", q);
  fetch(`/admin/threads?${params}`)
    .then((r) => r.json())
    .then((data) => {
      const list = $("#threadList");
      const items = data.threads || [];
      const total = data.total ?? items.length;
      list.innerHTML = "";
      if (!items.length) {
        list.innerHTML = `<div class="thread-item"><div class="empty">${
          q ? "无匹配会话" : "暂无会话<br>发送消息后会出现在这里（可切换回访）"
        }</div></div>`;
        return;
      }
      for (const t of items) {
        const item = document.createElement("div");
        item.className = "thread-item" + (t.thread_id === cur ? " active" : "");
        const title = t.first_message || t.thread_id;
        item.innerHTML =
          `<div class="tt">${esc(title)}${t.loaded === false ? ' <span class="arch">存档</span>' : ""}</div>` +
          `<div class="tid"><span class="tp">${esc(t.provider || "")}</span> ${esc(t.model || "")} · ${relTime(t.updated_at)}</div>`;
        item.onclick = () => switchThread(t.thread_id, t.loaded !== false, t.model);
        list.appendChild(item);
      }
      if (total > items.length) {
        const more = document.createElement("div");
        more.className = "thread-more";
        more.innerHTML = `仅显示最近 ${items.length} / 共 ${total} 条 · <a href="/ui/threads.html">会话页 →</a>`;
        list.appendChild(more);
      }
      return items;   // 返回列表（深链/绑定提示要用）
    })
    .catch(() => []);
}

function historyAtts(m) {
  return (m.attachments || [])
    .map((a) => ({
      name: a.name || "image",
      mime: a.mime || "",
      dataUrl: a.data ? `data:${a.mime || "application/octet-stream"};base64,${a.data}` : (a.url || ""),
    }))
    .filter((a) => a.dataUrl);
}

function renderHistory(msgs) {
  for (const m of msgs) {
    if (m.role === "user") {
      addMsg("user", m.content || "", { attachments: historyAtts(m) });
    } else {
      addMsg("assistant", m.content || "（空回复）", {
        thinking: m.reasoning || "", widgets: m.widgets || null,
      });
    }
  }
}

// 当前会话绑定的模型（同一 thread 不能换模型；换了就自动解绑成新会话）
let boundThreadModel = null;

function applyThreadModel(tid, model) {
  boundThreadModel = model || null;
  if (!model) return;
  const sel = $("#model");
  if ([...sel.options].some((o) => o.value === model) && sel.value !== model) sel.value = model;
  addMeta(`该会话绑定模型 ${model} · 续用只发最后一条；换模型会自动改为新会话`);
}

async function switchThread(tid, loaded = true, model = null) {
  applyThreadModel(tid, model);
  if (streaming) return;
  const el = $("#threadId");
  el.value = tid;
  el.dispatchEvent(new Event("input"));
  messages = [];
  $("#chat").innerHTML = "";
  addMeta(`已切换到会话 ${tid}`);
  try {
    const r = await fetch(`/admin/threads/${encodeURIComponent(tid)}/messages`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    const msgs = data.messages || [];
    if (msgs.length) {
      renderHistory(msgs);
    } else {
      addMeta(loaded ? "该会话暂无历史消息，新消息将续用该会话" : "已保存的会话，发送消息将自动恢复并继续");
    }
  } catch (e) {
    addMeta(`历史加载失败：${e.message}`);
  }
  refreshThreads();
}

function newChat() {
  if (streaming) return;
  $("#threadId").value = "";
  $("#threadId").dispatchEvent(new Event("input"));
  messages = [];
  $("#chat").innerHTML =
    '<div class="msg meta"><div class="bubble">已开始新会话（无 thread_id = 无状态，每次请求独立）</div></div>';
  refreshThreads();
}

$("#newChat").onclick = newChat;
$("#refreshThreads").onclick = refreshThreads;

// 换模型：若当前挂着绑定会话，自动解绑（否则服务端会 409 thread_mismatch）
$("#model").addEventListener("change", () => {
  const tid = $("#threadId").value.trim();
  const picked = $("#model").value;
  if (!tid || !boundThreadModel || picked === boundThreadModel) return;
  $("#threadId").value = "";
  $("#threadId").dispatchEvent(new Event("input"));
  addMeta(`已切换到 ${picked}：会话 ${tid} 绑定的是 ${boundThreadModel}，已自动改为新会话（同一会话不能换模型）`);
  boundThreadModel = null;
});

// ---- 工具条「更多」/ 侧栏搜索 / 原始报文 ----
$("#advToggle").onclick = () => document.querySelector(".toolbar").classList.toggle("expanded");
let threadSearchTimer = null;
$("#threadSearch").addEventListener("input", () => {
  clearTimeout(threadSearchTimer);
  threadSearchTimer = setTimeout(refreshThreads, 300);
});
$("#rawBtn").onclick = toggleRawPanel;

function curlFor(entry) {
  const body = JSON.stringify(entry.req || {});
  return `curl -s ${location.origin}/v1/chat/completions \\\n  -H 'Content-Type: application/json' \\\n  -d '${body.replace(/'/g, "'\\''")}'`;
}

// 原始请求/响应抽屉（rawLog 已有数据，之前没入口）
function toggleRawPanel() {
  const existing = $("#rawPanel");
  if (existing) { existing.remove(); return; }
  const panel = document.createElement("div");
  panel.id = "rawPanel";
  if (!rawLog.length) {
    panel.innerHTML = `<div class="raw-head">还没有请求记录</div>`;
  } else {
    const recent = rawLog.slice(-5).reverse();
    panel.innerHTML =
      `<div class="raw-head">最近 ${recent.length} 条请求（新→旧）</div>` +
      recent
        .map((e, i) => {
          const req = e.req || {};
          const res = e.res || { error: e.error };
          return `<div class="raw-item">
            <div class="raw-head">#${recent.length - i} · ${esc(req.model || "")}${
              e.error ? " · <span class=\"auth-expired\">失败</span>" : ""
            } <button class="btn sm" data-curl="${i}">复制为 curl</button></div>
            <pre class="raw">${esc(JSON.stringify({ request: req, response: res }, null, 2))}</pre>
          </div>`;
        })
        .join("");
    panel.querySelectorAll("[data-curl]").forEach((b) => {
      const e = recent[Number(b.dataset.curl)];
      b.onclick = () => copyText(curlFor(e));
    });
  }
  $("#layout").after(panel);
}

setupToolsPanel();
$("#attachBtn").onclick = () => $("#fileInput").click();
$("#fileInput").addEventListener("change", (e) => {
  for (const f of e.target.files) addAttachment(f);
  e.target.value = "";
});
loadModels();
refreshThreads();
$("#input").focus();

// 来自状态面板的「继续」链接：/ui/playground.html?thread_id=xxx
const qsThread = new URLSearchParams(location.search).get("thread_id");
if (qsThread) {
  $("#threadId").value = qsThread;
  $("#threadId").dispatchEvent(new Event("input"));
  // 容错：refreshThreads() 返回异常也不能连累整页（否则历史/正文都渲染不出来）
  (async () => {
    let items = [];
    try {
      items = (await refreshThreads()) || [];
    } catch (e) {
      console.warn("refreshThreads failed:", e);
    }
    const t = (Array.isArray(items) ? items : []).find((x) => x.thread_id === qsThread);
    switchThread(qsThread, t ? t.loaded !== false : true, t ? t.model : null);
  })();
}
