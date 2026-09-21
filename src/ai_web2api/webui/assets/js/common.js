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
