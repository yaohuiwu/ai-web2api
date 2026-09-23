/* 可复用「实时画面」组件（Playground 右栏 / 画面页共用）
 *
 * 能力：MJPEG 直播 + 状态轮询（会话/观众/fps/帧龄/页面 URL）+ 缩放（适应/100-200%）
 *      + 交互（点击/拖拽/输入/滚轮，需 server.live_control）+ 关遮挡 + 断线退避重试。
 *
 * 用法：
 *   const lv = LiveView.create({
 *     pane: document.querySelector('#livePane'),   // 容器（组件自建 DOM）
 *     getProvider: () => 'deepseek',               // 必填
 *     getThreadId: () => $('#threadId').value.trim(),
 *     allowControl: true,                          // 是否显示"交互"入口（服务端开关仍会二次校验）
 *     onClose: () => {},                           // 传了就显示 ✕（Playground 收起画面用）
 *     extraQuery: () => ({ fps: 5, quality: 75 }), // 额外查询参数（画面页的 fps/quality）
 *     headerExtra: element,                        // 额外头部控件（画面页的暂停/保存帧）
 *   });
 *   lv.setEnabled(true/false);  lv.refresh();  lv.destroy();
 */
window.LiveView = (function () {
  const ZOOM_KEY = "aiw2api_live_zoom";
  const CTRL_KEY = "aiw2api_live_ctrl";
  const DRAG_THRESHOLD_PX = 4;      // 位移阈值：≤4px 算点击，>4px 算拖拽（手抖不会误判成拖拽）

  const zoomPref = () => localStorage.getItem(ZOOM_KEY) || "fit";
  const ctrlPref = () => localStorage.getItem(CTRL_KEY) === "1";

  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  function create(opts) {
    const pane = opts.pane;
    if (!pane) throw new Error("LiveView: 缺少 pane 容器");
    const getProvider = opts.getProvider || (() => null);
    const getThreadId = opts.getThreadId || (() => "");
    const onToast = opts.onToast || (window.toast || function () {});

    // ---------- DOM ----------
    pane.innerHTML = "";
    const head = el("div", "lv-head");
    const title = el("span", "lv-title", "实时画面");
    const meta = el("span", "lv-meta mono");
    const spacer = el("span", "spacer");
    const zoomSel = el("select", "lv-sel");
    zoomSel.title = "缩放：适应=按宽度铺满；100% 以上可滚动（2x 截图放大后字更清楚）";
    for (const [v, t] of [["fit", "适应"], ["1", "100%"], ["1.5", "150%"], ["2", "200%"]]) {
      const o = el("option", null, t);
      o.value = v;
      zoomSel.appendChild(o);
    }
    const ctrlBtn = el("button", "btn sm lv-ctrl-toggle", "🖱 交互");
    ctrlBtn.title = "交互模式：在画面上点击/拖拽/输入（需 server.live_control: true）";
    const dismissBtn = el("button", "btn sm", "关遮挡");
    dismissBtn.title = "关闭页面上的弹窗/遮罩（挡住输入时用）";
    head.append(title, meta, spacer);
    if (opts.headerExtra) head.append(opts.headerExtra);
    head.append(zoomSel, ctrlBtn, dismissBtn);
    if (opts.onClose) {
      const closeBtn = el("button", "btn sm lv-close", "✕");
      closeBtn.title = "收起画面";
      closeBtn.onclick = () => opts.onClose();
      head.append(closeBtn);
    }

    const body = el("div", "lv-body");
    const img = el("img", "lv-img");
    img.alt = "实时画面";
    img.hidden = true;
    const guide = el("div", "lv-guide");
    guide.hidden = true;
    const hint = el("div", "lv-off");
    hint.hidden = true;
    body.append(img, guide, hint);

    const bar = el("div", "lv-ctrlbar");
    bar.hidden = true;
    const typeBox = el("input", "lv-type");
    typeBox.type = "text";
    typeBox.placeholder = "输入到页面（Enter 发送文字）";
    const typeSend = el("button", "btn sm", "输入");
    bar.append(typeBox, typeSend);
    for (const k of ["Enter", "Escape", "Tab", "Backspace", "ArrowUp", "ArrowDown"]) {
      const b = el("button", "btn sm", { Enter: "Enter", Escape: "Esc", Tab: "Tab", Backspace: "⌫", ArrowUp: "↑", ArrowDown: "↓" }[k]);
      b.dataset.key = k;
      b.title = k === "Escape" ? "Esc（可取消页面上卡住的状态）" : k;
      bar.append(b);
    }
    bar.append(el("span", "spacer"));
    const reloadBtn = el("button", "btn sm", "刷新");
    const bottomBtn = el("button", "btn sm", "回底");
    const resetBtn = el("button", "btn sm", "重置输入");
    resetBtn.title = "解开卡住的拖拽（发 up + Esc）";
    bar.append(reloadBtn, bottomBtn, resetBtn);

    pane.append(head, body, bar);

    // ---------- 状态 ----------
    let enabled = false;          // 面板是否显示
    let interactive = false;      // 交互模式
    let controlAllowed = null;    // 服务端是否允许交互（unknown 时先允许，403 会退回）
    let viewport = null;          // 页面 CSS 视口（坐标换算的权威来源）
    let available = null;         // provider 当前是否有打开的页面
    let shownProvider = null, shownThread = null;
    let stateTimer = null, retryTimer = null, pinTimer = null;
    let failStreak = 0, userScrolledUp = 0;
    let destroyed = false;

    const q = () => {
      const extra = (opts.extraQuery && opts.extraQuery()) || {};
      const params = new URLSearchParams();
      const tid = getThreadId();
      if (tid) params.set("thread_id", tid);
      for (const k of Object.keys(extra)) if (extra[k] != null && extra[k] !== "") params.set(k, extra[k]);
      return params.toString();
    };
    const streamUrl = (p) => `/admin/${encodeURIComponent(p)}/stream.mjpg?${q()}&t=${Date.now()}`;
    const stateUrl = (p) => `/admin/${encodeURIComponent(p)}/screen/state${getThreadId() ? "?thread_id=" + encodeURIComponent(getThreadId()) : ""}`;

    function setHint(text) {
      // 注意：**不删 <img>**（删了会在下次轮询时重建 → 断开/重连循环）
      if (text) {
        hint.hidden = false;
        hint.textContent = text;
        img.removeAttribute("src");
        img.hidden = true;
      } else {
        hint.hidden = true;
        hint.textContent = "";
      }
    }
    const showImg = () => { hint.hidden = true; img.hidden = false; };

    function applyZoom() {
      const z = zoomPref();
      if (z === "fit") {
        img.classList.add("fill");            // 按宽度铺满（消掉左右黑边；纵向超出可滚）
        img.style.width = "100%"; img.style.height = "auto";
        img.style.maxWidth = "none"; img.style.maxHeight = "none";
        return false;
      }
      img.classList.remove("fill");
      img.style.maxWidth = "none"; img.style.maxHeight = "none";
      img.style.width = `${parseFloat(z) * 100}%`;
      img.style.height = "auto";
      return true;
    }

    function pinBottom() {                    // 放大/拖动时跟随最新内容（用户刚滚过就不抢）
      if (!applyZoom()) return;
      if (Date.now() - userScrolledUp < 6000) return;
      body.scrollTop = body.scrollHeight;
    }

    function openStream(p) {
      if (!p) return;
      setHint(null);
      showImg();
      img.src = streamUrl(p);
      applyZoom();
      shownProvider = p;
      shownThread = getThreadId();
    }

    function scheduleRetry() {
      if (retryTimer) return;
      failStreak = Math.min(failStreak + 1, 5);
      const delay = Math.min(3000 * failStreak, 15000);     // 3s→6s→9s…最多 15s（退避）
      retryTimer = setTimeout(() => {
        retryTimer = null;
        shownProvider = null;
        sync();
      }, delay);
      meta.textContent = `画面重建中…（${Math.round(delay / 1000)}s 后重试）`;
    }

    function stopStream() {
      img.removeAttribute("src");
      img.hidden = true;
      img.onerror = null;
      if (stateTimer) { clearInterval(stateTimer); stateTimer = null; }
      if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
      if (pinTimer) { clearInterval(pinTimer); pinTimer = null; }
      shownProvider = shownThread = null;
      failStreak = 0;
    }

    async function pollState(p) {
      try {
        const res = await fetch(stateUrl(p));
        const st = await res.json();
        viewport = st.viewport || viewport;
        available = !!st.available;
        const bits = [];
        if (st.shown_thread_id) bits.push(`会话 ${st.shown_thread_id}`);
        if (st.viewers != null) bits.push(`观众 ${st.viewers}`);
        if (st.busy) bits.push("生成中");
        if (st.fps) bits.push(`${st.fps}fps`);
        if (st.last_frame_ago != null) bits.push(`帧龄 ${Number(st.last_frame_ago).toFixed(1)}s`);
        if (st.page_url) bits.push(String(st.page_url).replace(/^https?:\/\/[^/]+/, ""));
        if (st.error) bits.push(`⚠ ${st.error}`);
        meta.textContent = bits.join(" · ") || "—";
        if (!st.available) {
          setHint("暂无打开的页面 —— 在该 provider 上发一条消息，画面会自动出现");
        } else if (img.hidden || failStreak > 0) {
          if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
          failStreak = 0;
          openStream(p);
        }
      } catch (e) {
        meta.textContent = `状态不可用（${e.message}）`;
      }
    }

    function sync() {
      const p = getProvider();
      ctrlBtn.classList.toggle("active", ctrlPref());
      ctrlBtn.hidden = controlAllowed === false;     // 服务端关闭交互 → 藏起来（避免"点了没反应"）
      if (!enabled || !p || document.hidden) {
        pane.hidden = true;
        stopStream();
        return;
      }
      pane.hidden = false;
      if (controlAllowed === false && ctrlPref()) setCtrl(false);
      const tid = getThreadId();
      if (shownProvider !== p || shownThread !== tid) {
        title.textContent = `实时画面 · ${p}`;
        openStream(p);
      }
      if (!stateTimer) {
        pollState(p);
        stateTimer = setInterval(() => {
          const pp = getProvider();
          if (pp && !document.hidden) pollState(pp);
        }, 2500);
      }
    }

    // ---------- 交互（点击/拖拽/输入） ----------
    function pagePoint(ev) {
      if (!img.clientWidth) return null;
      const r = img.getBoundingClientRect();
      const vw = (viewport && viewport.width) || img.naturalWidth || 1440;
      const vh = (viewport && viewport.height) || img.naturalHeight || 900;
      return {
        x: Math.round(((ev.clientX - r.left) / r.width) * vw),
        y: Math.round(((ev.clientY - r.top) / r.height) * vh),
      };
    }

    async function postInput(payload) {
      const p = getProvider();
      if (!p) return null;
      const tid = getThreadId();
      try {
        const res = await fetch(`/admin/${encodeURIComponent(p)}/input${tid ? "?thread_id=" + encodeURIComponent(tid) : ""}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (res.status === 403) {
          controlAllowed = false;
          setCtrl(false);
          onToast("画面交互未开启（server.live_control: false）");
          return null;
        }
        if (!res.ok) {
          const j = await res.json().catch(() => ({}));
          onToast((j.error && j.error.message) || `输入失败（HTTP ${res.status}）`);
          return null;
        }
        return await res.json();
      } catch (e) {
        onToast("输入请求失败：" + e.message);
        return null;
      }
    }

    function setCtrl(on) {
      interactive = !!on;
      localStorage.setItem(CTRL_KEY, on ? "1" : "0");
      bar.hidden = !on;
      ctrlBtn.classList.toggle("active", !!on);
      img.classList.toggle("ctrl", !!on);
    }

    function flashAt(clientX, clientY) {      // 落点反馈：映射/遮挡问题一眼可见
      const r = img.getBoundingClientRect();
      const dot = el("div", "lv-dot");
      dot.style.left = `${clientX - r.left}px`;
      dot.style.top = `${clientY - r.top}px`;
      body.appendChild(dot);
      setTimeout(() => dot.remove(), 700);
    }

    let start = null, moved = false;
    img.addEventListener("pointerdown", (e) => {
      if (!interactive) return;               // 只读时完全不拦截
      const p = pagePoint(e);
      if (!p) return;
      start = { ...p, cx: e.clientX, cy: e.clientY };
      moved = false;
      try { img.setPointerCapture(e.pointerId); } catch (_) {}
      e.preventDefault();
    });
    img.addEventListener("pointermove", (e) => {
      if (!start || !interactive) return;
      if (Math.abs(e.clientX - start.cx) > DRAG_THRESHOLD_PX || Math.abs(e.clientY - start.cy) > DRAG_THRESHOLD_PX) moved = true;
      if (moved) {
        const ir = img.getBoundingClientRect();
        guide.style.left = `${Math.min(start.cx, e.clientX) - ir.left}px`;
        guide.style.top = `${Math.min(start.cy, e.clientY) - ir.top}px`;
        guide.style.width = `${Math.abs(e.clientX - start.cx)}px`;
        guide.style.height = `${Math.abs(e.clientY - start.cy)}px`;
        guide.hidden = false;
      }
    });
    img.addEventListener("pointerup", async (e) => {
      if (!start || !interactive) return;
      const s = start, wasMoved = moved, end = pagePoint(e);
      start = null; moved = false; guide.hidden = true;
      if (!end) return;
      flashAt(e.clientX, e.clientY);
      if (wasMoved) await postInput({ action: "drag", x: s.x, y: s.y, x2: end.x, y2: end.y });
      else await postInput({ action: "click", x: end.x, y: end.y });
    });
    img.addEventListener("wheel", (e) => {
      if (!interactive) return;
      const canScrollPane = body.scrollHeight > body.clientHeight + 4;
      // 默认滚面板；面板没得滚（或 Shift）→ 发给页面（否则用户会觉得"滚动没反应"）
      if (!e.shiftKey && canScrollPane) return;
      e.preventDefault();
      const p = pagePoint(e);
      postInput({ action: "wheel", x: p ? p.x : 0, y: p ? p.y : 0, dx: e.deltaX, dy: e.deltaY });
    }, { passive: false });

    const sendType = () => {
      const text = typeBox.value;
      if (!text) return;
      typeBox.value = "";
      postInput({ action: "type", text });
    };
    typeSend.onclick = sendType;
    typeBox.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendType(); }
    });
    bar.querySelectorAll("[data-key]").forEach((b) => {
      b.onclick = () => postInput({ action: "key", key: b.dataset.key });
    });
    reloadBtn.onclick = () => postInput({ action: "reload" });
    bottomBtn.onclick = () => postInput({ action: "to_bottom" });
    resetBtn.onclick = async () => {          // 解卡：发 up + Esc
      await postInput({ action: "up", x: 0, y: 0 });
      await postInput({ action: "key", key: "Escape" });
      onToast("已重置输入状态");
    };
    ctrlBtn.onclick = () => setCtrl(!ctrlPref());
    dismissBtn.onclick = async () => {
      const p = getProvider();
      if (!p) return;
      try {
        const res = await fetch(`/admin/${encodeURIComponent(p)}/dismiss`, { method: "POST" });
        const j = await res.json().catch(() => ({}));
        onToast(j.dismissed && j.dismissed.length ? `已关闭 ${j.dismissed.length} 个弹窗` : "没有发现可关闭的弹窗");
      } catch (e) {
        onToast("关闭失败：" + e.message);
      }
    };
    zoomSel.onchange = () => { localStorage.setItem(ZOOM_KEY, zoomSel.value); applyZoom(); pinBottom(); };
    body.addEventListener("scroll", () => {
      if (body.scrollHeight - body.scrollTop - body.clientHeight > 24) userScrolledUp = Date.now();
    });
    img.onerror = () => {
      setHint("画面暂不可用（该 provider 当前没有打开的页面）—— 发一条消息后会自动出现");
      scheduleRetry();
    };
    img.onload = () => {
      showImg();
      if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
      failStreak = 0;
      pinBottom();
    };
    document.addEventListener("visibilitychange", () => sync());

    // ---------- 初始 ----------
    zoomSel.value = zoomPref();
    setCtrl(ctrlPref());
    pinTimer = setInterval(pinBottom, 1000);
    applyZoom();

    return {
      root: pane,
      setEnabled(v) { enabled = !!v; sync(); },
      setControlAllowed(v) { controlAllowed = v; sync(); },
      refresh() { shownProvider = null; sync(); },
      state: { get viewport() { return viewport; }, get available() { return available; } },
      destroy() {
        destroyed = true;
        stopStream();
        document.removeEventListener("visibilitychange", sync);
        pane.innerHTML = "";
      },
      _isDestroyed: () => destroyed,
    };
  }

  return { create };
})();
