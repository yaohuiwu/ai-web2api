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
    syncLive();                       // 页面已就绪 → 右侧画面立刻跟上这次会话
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

// ---------- 实时画面（右栏，只读直播）：边聊边对，便于定位"是不是站点侧慢" ----------
// 设计复用"画面"页（docs/LIVE_VIEW.md）：MJPEG <img> + 状态轮询；只在面板可见时才开流。
let PROVIDER_MAP = {};        // 模型名 → provider（来自 /admin/status）
let liveStateTimer = null;
let liveProviderShown = null;
let liveThreadShown = null;

const liveOn = () => localStorage.getItem("aiw2api_live") !== "0";
const liveThread = () => $("#threadId").value.trim();
const liveProvider = () => PROVIDER_MAP[$("#model").value] || null;

const liveZoom = () => localStorage.getItem("aiw2api_live_zoom") || "fit";   // 画面一律**整页**

function liveStreamUrl(p, tid) {
  const t = tid ? `thread_id=${encodeURIComponent(tid)}&` : "";
  return `/admin/${encodeURIComponent(p)}/stream.mjpg?${t}crop=full&t=${Date.now()}`;
}

// 缩放：fit = 整幅可见；否则按百分比显示（可滚动）。CSS 像素不变、截图 2x → 放大后字很锐利。
function applyLiveZoom() {
  const img = $("#live");
  if (!img) return false;
  const z = liveZoom();
  if (z === "fit") {
    // 适应 = **按宽度铺满**（消掉左右黑边；纵向超出由面板滚动）
    img.style.maxWidth = "none"; img.style.maxHeight = "none";
    img.style.width = "100%"; img.style.height = "auto";
    img.classList.add("fill");
  } else {
    img.style.maxWidth = "none"; img.style.maxHeight = "none";
    img.style.width = `${parseFloat(z) * 100}%`; img.style.height = "auto";
    img.classList.remove("fill");
  }
  return z !== "fit";
}

let liveUserScrolledUp = 0;
function pinLiveBottom() {
  const body = document.querySelector(".live-body");
  if (!body || !applyLiveZoom()) return;
  if (Date.now() - liveUserScrolledUp < 6000) return;    // 用户刚手动滚动过 → 不抢
  body.scrollTop = body.scrollHeight;                     // 跟随生成：始终看最新内容
}
function liveStateUrl(p, tid) {
  return `/admin/${encodeURIComponent(p)}/screen/state${tid ? `?thread_id=${encodeURIComponent(tid)}` : ""}`;
}

async function loadProviderMap() {
  try {
    const st = await (await fetch("/admin/status")).json();
    PROVIDER_MAP = {};
    for (const p of st.providers || []) {
      for (const m of p.models || []) PROVIDER_MAP[m] = p.name;
    }
  } catch (e) {
    console.warn("loadProviderMap failed:", e);
    PROVIDER_MAP = {};
  }
  syncLive();
}

let liveAvailable = null;          // 该 provider 当前是否有打开的页面（无页面 → 503）
let liveViewport = null;           // 页面 CSS 视口（来自 /screen/state）→ 坐标换算
let liveInteractive = false;       // 交互模式（默认关；需 server.live_control: true）
let liveRetryTimer = null;

function showLiveHint(msg) {
  const body = document.querySelector(".live-body");
  if (!body) return;
  body.innerHTML = `<div class="live-off">${esc(msg)}</div>`;
  liveAvailable = false;
}

function openLiveStream(p, tid) {
  const body = document.querySelector(".live-body");
  if (!body) return;
  if (!$("#live")) body.innerHTML = '<img id="live" alt="实时画面（只读）" hidden>';
  const img = $("#live");
  img.hidden = false;
  img.onerror = () => {                 // 流被拒（多为"没有打开的页面"）→ 提示 + 稍后重试
    showLiveHint("画面暂不可用（该 provider 当前没有打开的页面）—— 发一条消息后会自动出现");
    scheduleLiveRetry();
  };
  img.src = liveStreamUrl(p, tid);
  applyLiveZoom();
  liveProviderShown = p;
  liveThreadShown = tid;
}

function stopLive() {
  const img = $("#live");
  if (img) { img.removeAttribute("src"); img.hidden = true; img.onerror = null; }
  if (liveStateTimer) { clearInterval(liveStateTimer); liveStateTimer = null; }
  if (liveRetryTimer) { clearTimeout(liveRetryTimer); liveRetryTimer = null; }
  liveProviderShown = liveThreadShown = null;
}

function scheduleLiveRetry() {
  if (liveRetryTimer) return;
  liveRetryTimer = setTimeout(() => {
    liveRetryTimer = null;
    liveProviderShown = null;           // 强制重开流
    syncLive();
  }, 3000);
}

async function pollLiveState(p, tid) {
  try {
    const st = await (await fetch(liveStateUrl(p, tid))).json();
    const bits = [];
    if (st.shown_thread_id) bits.push(`会话 ${st.shown_thread_id}`);
    if (st.viewers != null) bits.push(`观众 ${st.viewers}`);
    if (st.busy) bits.push("生成中");
    if (st.fps) bits.push(`${st.fps}fps`);
    if (st.page_url) bits.push(String(st.page_url).replace(/^https?:\/\/[^/]+/, ""));
    $("#liveMeta").textContent = bits.join(" · ") || "—";
    liveAvailable = !!st.available;
    liveViewport = st.viewport || liveViewport;
    if (!st.available) {
      // 没有打开的页面：不发流（省资源），给出可行动提示；页面一出现（发消息后）自动恢复
      if ($("#live")) { const img = $("#live"); img.removeAttribute("src"); img.hidden = true; }
      showLiveHint("暂无打开的页面 —— 在该 provider 上发一条消息，画面会自动出现");
    } else if (!$("#live") || !liveProviderShown) {
      openLiveStream(p, tid);
    }
  } catch (e) {
    $("#liveMeta").textContent = "状态不可用";
  }
}

// 同步画面：模型/thread_id/开关/可见性变化时重开流
function syncLive() {
  const pane = $("#livePane");
  if (!pane) return;
  const p = liveProvider();
  $("#liveToggle").classList.toggle("primary", liveOn() && !!p);
  if (!liveOn() || !p || document.hidden) {
    pane.hidden = true;
    const handle = $("#splitHandle");
    if (handle) handle.hidden = true;
    stopLive();
    return;
  }
  pane.hidden = false;
  const handle = $("#splitHandle");
  if (handle) handle.hidden = false;
  applyLiveWidth();
  const tid = liveThread();
  if (liveProviderShown !== p || liveThreadShown !== tid) {
    $("#liveTitle").textContent = `实时画面 · ${p}`;
    openLiveStream(p, tid);             // 可能 503 → onerror 会给提示并自动重试
  }
  if (!liveStateTimer) {
    pollLiveState(p, tid);
    liveStateTimer = setInterval(() => {
      if (document.hidden || !liveProvider()) return;
      pollLiveState(liveProvider(), liveThread());
    }, 2500);
  }
}

function initLivePane() {
  const toggle = $("#liveToggle");
  if (!toggle) return;
  toggle.onclick = () => {
    localStorage.setItem("aiw2api_live", liveOn() ? "0" : "1");
    const pane = $("#livePane");
    if (!liveOn()) { pane.hidden = true; stopLive(); }
    syncLive();
  };
  $("#liveClose").onclick = () => {
    localStorage.setItem("aiw2api_live", "0");
    $("#livePane").hidden = true;
    stopLive();
    syncLive();
  };
  $("#liveDismiss").onclick = async () => {
    const p = liveProvider();
    if (!p) return;
    try {
      await fetch(`/admin/${encodeURIComponent(p)}/dismiss`, { method: "POST" });
      toast("已尝试关闭页面遮挡");
    } catch (e) {
      toast("关闭失败：" + e.message);
    }
  };
  initSplitHandle();

  // 模型/会话变化 → 画面跟着切（会话变化时钉到该会话页面）
  $("#model").addEventListener("change", () => syncLive());
  $("#liveZoom").addEventListener("change", () => {
    localStorage.setItem("aiw2api_live_zoom", $("#liveZoom").value);
    applyLiveZoom();
    pinLiveBottom();
  });
  const liveBody = document.querySelector(".live-body");
  if (liveBody) liveBody.addEventListener("scroll", () => {
    const atBottom = liveBody.scrollHeight - liveBody.scrollTop - liveBody.clientHeight < 24;
    if (!atBottom) liveUserScrolledUp = Date.now();
  });
  setInterval(pinLiveBottom, 1000);           // 跟随生成（放大时自动滚到底）
  $("#threadId").addEventListener("input", () => {
    liveThreadShown = null;          // 强制重开流（?thread_id= 变化）
    syncLive();
  });
  document.addEventListener("visibilitychange", () => syncLive());
  const zoomSel = $("#liveZoom");
  if (zoomSel) zoomSel.value = liveZoom();
  loadProviderMap();
}
// ---------- 画面交互（control）：点击 / 拖拽 / 输入 / 滚轮（默认关） ----------
// 坐标换算：只认**图片像素 → 页面 CSS 像素**（比例换算，与 device_scale_factor 无关）。
// 反复强调的两条防护：① 位移 >4px 才算拖拽（否则手抖会把"点击"变成拖拽）；
// ② 「重置输入」发 up + Esc（万一某次拖拽没松开，避免后续点击全乱）。
const CTRL_KEY = "aiw2api_live_ctrl";
const DRAG_THRESHOLD_PX = 4;

function ctrlOn() { return localStorage.getItem(CTRL_KEY) === "1"; }

function livePagePoint(ev) {
  const img = $("#live");
  if (!img || !img.clientWidth) return null;
  const r = img.getBoundingClientRect();
  const vw = (liveViewport && liveViewport.width) || img.naturalWidth || 1440;
  const vh = (liveViewport && liveViewport.height) || img.naturalHeight || 900;
  return {
    x: Math.round(((ev.clientX - r.left) / r.width) * vw),
    y: Math.round(((ev.clientY - r.top) / r.height) * vh),
  };
}

async function postInput(payload) {
  const p = liveProvider();
  if (!p) return null;
  const tid = liveThread();
  const q = tid ? `?thread_id=${encodeURIComponent(tid)}` : "";
  try {
    const res = await fetch(`/admin/${encodeURIComponent(p)}/input${q}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.status === 403) {
      setCtrl(false);
      toast("画面交互未开启（server.live_control: false）");
      return null;
    }
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      toast((j.error && j.error.message) || `输入失败（HTTP ${res.status}）`);
      return null;
    }
    return await res.json();
  } catch (e) {
    toast("输入请求失败：" + e.message);
    return null;
  }
}

function setCtrl(on) {
  liveInteractive = !!on;
  localStorage.setItem(CTRL_KEY, on ? "1" : "0");
  const bar = $("#liveCtrlBar"), img = $("#live"), btn = $("#liveCtrl");
  if (bar) bar.hidden = !on;
  if (btn) btn.classList.toggle("active", !!on);
  if (img) img.classList.toggle("ctrl", !!on);
  if (!on && img) img.style.cursor = "";
}

function showGuide(x1, y1, x2, y2) {
  const g = $("#liveGuide"), img = $("#live");
  if (!g || !img) return;
  const r = img.getBoundingClientRect(), body = img.parentElement.getBoundingClientRect();
  const l = Math.min(x1, x2), t = Math.min(y1, y2);
  g.style.left = `${l - body.left}px`;
  g.style.top = `${t - body.top}px`;
  g.style.width = `${Math.abs(x2 - x1)}px`;
  g.style.height = `${Math.abs(y2 - y1)}px`;
  g.style.display = "block";
  void r;
}

function hideGuide() { const g = $("#liveGuide"); if (g) g.style.display = "none"; }

function initLiveControl() {
  const img = $("#live"), bar = $("#liveCtrlBar");
  if (!img || !bar) return;
  setCtrl(ctrlOn());

  $("#liveCtrl").onclick = () => setCtrl(!ctrlOn());

  let start = null, moved = false;

  img.addEventListener("pointerdown", (e) => {
    if (!liveInteractive) return;                       // 只读模式：完全不拦截
    const p = livePagePoint(e);
    if (!p) return;
    start = { ...p, cx: e.clientX, cy: e.clientY };
    moved = false;
    try { img.setPointerCapture(e.pointerId); } catch (_) {}
    e.preventDefault();
  });

  img.addEventListener("pointermove", (e) => {
    if (!start || !liveInteractive) return;
    const dx = Math.abs(e.clientX - start.cx), dy = Math.abs(e.clientY - start.cy);
    if (dx > DRAG_THRESHOLD_PX || dy > DRAG_THRESHOLD_PX) moved = true;
    if (moved) showGuide(start.cx, start.cy, e.clientX, e.clientY);
  });

  img.addEventListener("pointerup", async (e) => {
    if (!start || !liveInteractive) return;
    const end = livePagePoint(e);
    const wasMoved = moved;
    const s = start;
    start = null; moved = false;
    hideGuide();
    if (!end) return;
    if (wasMoved) {
      await postInput({ action: "drag", x: s.x, y: s.y, x2: end.x, y2: end.y });
    } else {
      await postInput({ action: "click", x: end.x, y: end.y });
    }
  });

  // 滚轮：默认滚**面板**；Shift+滚轮才发给页面（避免误操作站点）
  img.addEventListener("wheel", (e) => {
    if (!liveInteractive || !e.shiftKey) return;
    e.preventDefault();
    const p = livePagePoint(e);
    postInput({ action: "wheel", x: p ? p.x : 0, y: p ? p.y : 0, dx: e.deltaX, dy: e.deltaY });
  }, { passive: false });

  // 输入框 + 快捷键
  const typeBox = $("#liveType");
  const sendType = () => {
    const text = typeBox.value;
    if (!text) return;
    typeBox.value = "";
    postInput({ action: "type", text });
  };
  $("#liveTypeSend").onclick = sendType;
  typeBox.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendType(); }
  });
  bar.querySelectorAll("[data-key]").forEach((b) => {
    b.onclick = () => postInput({ action: "key", key: b.getAttribute("data-key") });
  });
  $("#liveReload").onclick = () => postInput({ action: "reload" });
  $("#liveBottom").onclick = () => postInput({ action: "to_bottom" });
  $("#liveResetInput").onclick = async () => {           // 解卡：发 up + Esc
    await postInput({ action: "up", x: 0, y: 0 });
    await postInput({ action: "key", key: "Escape" });
    toast("已重置输入状态");
  };
}
initLiveControl();

// ---------- 中缝拖拽：左右调整"对话 / 画面"宽度（双击复位，宽度记忆） ----------
const LIVE_W_KEY = "aiw2api_live_width";

function defaultLiveWidth() {
  const split = $("#split");
  return Math.round((split ? split.clientWidth : 1440) * 0.42);
}

function applyLiveWidth(w) {
  const pane = $("#livePane"), split = $("#split");
  if (!pane) return;
  const saved = w != null ? w : parseInt(localStorage.getItem(LIVE_W_KEY) || "", 10);
  if (!saved || !split) return;                      // 没设过就用 CSS 默认
  const max = Math.max(260, split.clientWidth - 320);   // 左边至少留 320px 给对话
  pane.style.width = `${Math.min(Math.max(260, saved), max)}px`;
  pane.style.minWidth = "260px";
  pane.style.maxWidth = "none";
}

function initSplitHandle() {
  const handle = $("#splitHandle"), pane = $("#livePane"), split = $("#split");
  if (!handle || !pane || !split) return;

  let startX = 0, startW = 0, dragging = false;

  const endDrag = (save) => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove("dragging");
    // ⚠️ 一定要把 window 级监听摘干净：否则"拖拽后普通点击/移动"会被当成继续拖拽
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onCancel);
    window.removeEventListener("blur", onCancel);
    if (save) localStorage.setItem(LIVE_W_KEY, String(Math.round(pane.getBoundingClientRect().width)));
  };
  const onMove = (e) => {
    if (!dragging) return;
    if (e.buttons === 0) { endDrag(true); return; }        // 鼠标已在别处松开 → 立即结束
    const max = Math.max(260, split.clientWidth - 320);
    const next = Math.min(Math.max(260, startW - (e.clientX - startX)), max);
    pane.style.width = `${next}px`;
    pane.style.minWidth = "260px";
    pane.style.maxWidth = "none";
  };
  const onUp = () => endDrag(true);
  const onCancel = () => endDrag(true);                   // pointercancel / 窗口失焦

  handle.addEventListener("pointerdown", (e) => {
    if (pane.hidden) return;
    dragging = true;
    startX = e.clientX;
    startW = pane.getBoundingClientRect().width;
    handle.classList.add("dragging");
    try { handle.setPointerCapture(e.pointerId); } catch (_) {}   // 保证一定收到 pointerup
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onCancel);
    window.addEventListener("blur", onCancel);
    e.preventDefault();                                   // 拖拽时不要选中文本
  });
  handle.addEventListener("dblclick", () => {                 // 双击复位
    endDrag(false);
    localStorage.removeItem(LIVE_W_KEY);
    pane.style.width = "";
    pane.style.minWidth = "";
    pane.style.maxWidth = "";
    applyLiveWidth(defaultLiveWidth());
  });
  handle.addEventListener("keydown", (e) => {                 // ←/→ 微调 40px
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const cur = pane.getBoundingClientRect().width;
    applyLiveWidth(cur + (e.key === "ArrowLeft" ? 40 : -40));
    e.preventDefault();
  });
  applyLiveWidth();
}

initLivePane();

