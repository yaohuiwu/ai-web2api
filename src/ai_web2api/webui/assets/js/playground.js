// Playground 逻辑
let history = [];       // [{role, content}]
let rawLog = [];        // [{req, res|error}]
let streaming = false;

const apiKeyEl = $("#apiKey");
apiKeyEl.value = localStorage.getItem("aiw2api_api_key") || "";
apiKeyEl.addEventListener("change", () =>
  localStorage.setItem("aiw2api_api_key", apiKeyEl.value));

function addMsg(role, content, opts = {}) {
  const chat = $("#chat");
  const div = document.createElement("div");
  div.className = "msg " + (opts.error ? "error" : role);
  let inner = `<div class="bubble">`;
  if (opts.thinking && opts.thinking.trim()) {
    inner += `<details class="thinking"><summary>💭 思考过程</summary>${md(opts.thinking)}</details>`;
  }
  inner += md(content) + `</div>`;
  div.innerHTML = inner;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

function addMeta(text) {
  const chat = $("#chat");
  const div = document.createElement("div");
  div.className = "msg meta";
  div.innerHTML = `<div class="bubble">${esc(text)}</div>`;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

function showRaw(title) {
  const chat = $("#chat");
  const div = document.createElement("div");
  div.className = "msg meta";
  div.innerHTML = `<details class="raw"><summary>${esc(title)}</summary><pre class="raw"></pre></details>`;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div.querySelector("pre");
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

function buildBody(content) {
  const body = {
    model: $("#model").value,
    messages: [...history, { role: "user", content }],
    stream: $("#stream").checked,
    // 开关显式发送：DeepSeek 三模式合并后它们就是唯一的输出控制项
    // （新版页面默认两者都开，与这里的勾选默认值一致）
    deep_think: $("#deepThink").checked,
    search: $("#search").checked,
  };
  const tid = $("#threadId").value.trim();
  if (tid) body.thread_id = tid;
  const mode = $("#mode").value;
  if (mode) body.mode = mode;
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
// （否则每条消息都会被当成新会话 → thread_id 每轮都变，上下文无法延续）
function rememberThreadId(tid) {
  if (!tid) return;
  const el = $("#threadId");
  if (!el || el.value.trim()) return;   // 元素缺失 / 用户已显式填了 thread_id → 不覆盖
  el.value = tid;
  el.dispatchEvent(new Event("input"));  // 触发 hint 更新
  refreshThreads();                      // 左侧列表立即高亮该会话
}

async function sendNonStream(body) {
  const res = await fetch("/v1/chat/completions", {
    method: "POST", headers: headers(), body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(errMsg(data));
  rawLog.push({ req: body, res: data });
  const msg = data.choices && data.choices[0] && data.choices[0].message;
  const content = msg ? msg.content : "";
  const th = msg ? msg.reasoning_content : null;
  if (th && !content) {
    // 长思考被稳定判定截断（完整() 返回时正文未生成）→ 明确提示
    addMsg("assistant", "（思考过程已完整输出，但正文未生成——可能因思考过长被结束判定截断，请重试）", { thinking: th });
  } else {
    addMsg("assistant", content || "（空回复）", { thinking: th });
  }
  if (data.thread_id) {
    addMeta(`thread_id: ${data.thread_id}`);
    rememberThreadId(data.thread_id);
  }
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
  const bubbleDiv = addMsg("assistant", "");
  const bubble = bubbleDiv.querySelector(".bubble");
  // 正文独立容器：流式更新只写这里，绝不触碰思考块（textContent 整体覆盖会删掉 thinking）
  const contentBox = document.createElement("div");
  contentBox.className = "content";
  bubble.appendChild(contentBox);
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
    if (delta.reasoning_content) {
      fullThinking += delta.reasoning_content;
      ensureThinkBox().appendChild(document.createTextNode(delta.reasoning_content));
    }
    if (delta.content) {
      fullContent += delta.content;
      contentBox.textContent = fullContent;
      $("#chat").scrollTop = $("#chat").scrollHeight;
    }
  };

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
      const delta = obj.choices && obj.choices[0] && obj.choices[0].delta;
      if (delta) flushChunk(delta);
      if (obj.thread_id && !gotThreadId) {
        gotThreadId = true;
        addMeta(`thread_id: ${obj.thread_id}`);
        rememberThreadId(obj.thread_id);
      }
    }
  }
  // 收尾：md 渲染（流式期间 textContent 保证顺滑，结束后一次性渲染）
  if (thinkBox && !fullThinking) {
    thinkBox.remove();
  } else if (thinkBox) {
    const open = thinkBox.open;
    thinkBox.innerHTML = "<summary>💭 思考过程</summary>" + md(fullThinking);
    thinkBox.open = open;
  }
  rawLog.push({ req: body, res: { content: fullContent, thinking: fullThinking } });
  if (!fullContent && fullThinking) {
    // 真实 DeepSeek 长思考时，稳定判定可能在正文开始前结束流（已知行为）→ 明确提示
    contentBox.innerHTML = "<p>（思考过程已完整输出，但正文未生成——可能因思考过长被结束判定截断，请重试）</p>";
  } else if (!fullContent && !fullThinking) {
    contentBox.textContent = "（空回复）";
  } else {
    contentBox.innerHTML = md(fullContent);
    // 服务端错误透传（[会话错误]/[错误] 前缀）→ 红色气泡
    if (/^\s*\[(会话错误|错误)\]/.test(fullContent)) {
      bubble.classList.add("error");
      contentBox.style.color = "#dc2626";
    }
  }
}

async function send() {
  const text = $("#input").value.trim();
  if (!text || streaming) return;
  const body = buildBody(text);
  history.push({ role: "user", content: text });
  addMsg("user", text);
  localStorage.setItem("aiw2api_model", body.model);
  const tidUsed = body.thread_id || "";
  if (tidUsed) addMeta(`thread_id: ${tidUsed}（绑定会话，续用只发最后一条）`);
  $("#input").value = "";
  $("#send").disabled = true;
  streaming = true;
  try {
    if (body.stream) await sendStream(body);
    else await sendNonStream(body);
    $("#hint").textContent = "";
  } catch (e) {
    addMsg("error", "请求失败: " + e.message);
    rawLog.push({ req: body, error: e.message });
    $("#hint").textContent = "提示：503 = 未登录；429 = 队列满/thread 超限；409 = thread 冲突或失效";
  } finally {
    streaming = false;
    $("#send").disabled = false;
    $("#input").focus();
    refreshThreads(); // 新 thread 创建后立即出现在左侧
  }
}

$("#send").onclick = send;
$("#clear").onclick = () => {
  history = [];
  $("#chat").innerHTML = `<div class="msg meta"><div class="bubble">已清空。${$("#threadId").value.trim() ? "（thread_id 仍保留，可继续原会话）" : ""}</div></div>`;
};
// IME（输入法）组词保护：中文/日文等组词时按回车是「选词上屏」，不应发送。
// 部分 IME 确认候选词时 isComposing 已置 false → 再用 compositionend 时间戳兜底。
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
  // 组词中（isComposing / keyCode 229 / composing）或刚结束组词 → 这次回车是选词
  if (e.isComposing || e.keyCode === 229 || composing) return;
  if (Date.now() - composeEndedAt < 80) return;
  e.preventDefault();
  send();
});
$("#threadId").addEventListener("change", () => { history = []; });
$("#threadId").addEventListener("input", (e) => {
  const tid = e.target.value.trim();
  // mode 在新版 UI 里等价于开关组合（服务端把 fast/expert 翻译成 深度思考/智能搜索），
  // 开关每次请求都能改 → 绑定 thread 后依旧可切换，不再禁用
  $("#hint").textContent = tid
    ? "已启用会话绑定：首次请求创建会话并注入全部历史，后续 resume 只发最后一条；深度思考/智能搜索可随时切换"
    : "";
});
$("#mode").addEventListener("change", (e) => {
  // mode 预设同步到开关：避免"mode=fast 但深度思考还勾着"这种矛盾参数
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

// ---- 左侧会话列表（GET /admin/threads 轮询；点击切换 thread）----
function refreshThreads() {
  fetch("/admin/threads")
    .then((r) => r.json())
    .then((data) => {
      const cur = $("#threadId").value.trim();
      const list = $("#threadList");
      const items = data.threads || [];
      list.innerHTML = "";
      if (!items.length) {
        list.innerHTML =
          '<div class="thread-item"><div class="empty">暂无会话<br>发送消息后会出现在这里（可切换回访）</div></div>';
        return;
      }
      for (const t of items) {
        const item = document.createElement("div");
        item.className = "thread-item" + (t.thread_id === cur ? " active" : "");
        const title = t.first_message || t.thread_id;
        item.innerHTML =
          `<div class="tt">${esc(title)}${t.loaded === false ? ' <span class="arch">存档</span>' : ""}</div>` +
          `<div class="tid">${esc(t.thread_id)} · ${esc(t.model || "")}</div>`;
        item.onclick = () => switchThread(t.thread_id, t.loaded !== false);
        list.appendChild(item);
      }
    })
    .catch(() => {});
}

function renderHistory(msgs) {
  for (const m of msgs) {
    if (m.role === "user") {
      addMsg("user", m.content || "");
    } else {
      addMsg("assistant", m.content || "（空回复）", { thinking: m.reasoning || "" });
    }
  }
}

async function switchThread(tid, loaded = true) {
  if (streaming) return;
  const el = $("#threadId");
  el.value = tid;
  el.dispatchEvent(new Event("input")); // 触发 hint 更新
  history = [];
  $("#chat").innerHTML = "";
  addMeta(`已切换到会话 ${tid}`);
  try {
    // 从服务端拉历史消息（SQLite），切换后即可回看
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
  history = [];
  $("#chat").innerHTML =
    '<div class="msg meta"><div class="bubble">已开始新会话（无 thread_id = 无状态，每次请求独立）</div></div>';
  refreshThreads();
}

$("#newChat").onclick = newChat;
$("#refreshThreads").onclick = refreshThreads;

loadModels();
refreshThreads();
$("#input").focus();
