// 公共前端工具：DOM 选择 / HTML 转义 / JSON API 封装（各页面复用）
const $ = (s) => document.querySelector(s);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch { /* 非 JSON */ }
  if (!res.ok) {
    const msg = body && body.error && body.error.message ? body.error.message
      : (body && body.detail ? JSON.stringify(body.detail) : `HTTP ${res.status}`);
    throw new Error(msg);
  }
  return body;
}

// 仓库地址：UI 顶部 Star 入口 + 页脚链接（后端 /admin/repo 会回传真实地址，保持同源）
const REPO_URL = "https://github.com/yaohuiwu/ai-web2api";

// toast：页面没有 .toast 元素时自动创建（两页共用）
function toast(msg) {
  let el = document.querySelector(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 2600);
}

// 复制到剪贴板（带 toast 反馈）
function copyText(t) {
  const text = String(t ?? "");
  const ok = () => toast("已复制");
  const bad = () => toast("复制失败，请手动选择");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(ok, bad);
  } else {
    toast("浏览器不支持自动复制，请手动选择");
  }
}

// footer 内容（版本/文档/健康检查/模型列表；GitHub 视 REPO_URL 而定）
function renderFooter() {
  const el = document.querySelector("footer[data-footer]");
  if (!el) return;
  const repo = REPO_URL
    ? `<a href="${REPO_URL}" target="_blank" rel="noopener">GitHub</a>`
    : "";
  el.innerHTML =
    `<span>ai-web2api <span class="f-ver" data-version>v0.1.0</span></span>
     <span class="f-links">
       <a href="/docs" target="_blank" rel="noopener">API 文档</a>
       <a href="/openapi.json" target="_blank" rel="noopener">OpenAPI</a>
       <a href="/healthz" target="_blank" rel="noopener">健康检查</a>
       <a href="/v1/models" target="_blank" rel="noopener">模型列表</a>
       ${repo}
     </span>`;
}
document.addEventListener("DOMContentLoaded", renderFooter);

// 顶部 GitHub Star 徽章：星标数由后端带缓存拉取（GitHub 匿名限流），
// 失败/未配置时只显示 "Star" 文案 —— 链接始终可用，不影响界面。
function renderRepoBadge() {
  const el = document.querySelector("[data-gh-star]");
  if (!el) return;
  el.href = REPO_URL;
  const count = el.querySelector(".gh-count");
  fetch("/admin/repo")
    .then((r) => r.json())
    .then((d) => {
      if (d && d.url) el.href = d.url;
      if (count) count.textContent = d && d.stars != null ? String(d.stars) : "Star";
    })
    .catch(() => {
      if (count) count.textContent = "Star";
    });
}
document.addEventListener("DOMContentLoaded", renderRepoBadge);
