// 主题：优先用户选择（localStorage），否则跟随系统。放在 <head> 尽早执行，避免闪屏。
(function () {
  const KEY = "aiw2api_theme";
  const root = document.documentElement;
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");

  const initial = localStorage.getItem(KEY) || (prefersDark && prefersDark.matches ? "dark" : "light");
  root.dataset.theme = initial;

  window.currentTheme = () => root.dataset.theme || "light";
  window.setTheme = (t) => {
    root.dataset.theme = t;
    localStorage.setItem(KEY, t);
    document.dispatchEvent(new CustomEvent("themechange", { detail: t }));
  };
  window.toggleTheme = () => {
    window.setTheme(window.currentTheme() === "dark" ? "light" : "dark");
    return window.currentTheme();
  };

  // 同步所有切换按钮的图标/无障碍标签
  window.syncThemeButtons = function () {
    const dark = window.currentTheme() === "dark";
    document.querySelectorAll("[data-theme-toggle]").forEach((b) => {
      const label = dark ? "切换到亮色主题" : "切换到暗色主题";
      b.textContent = dark ? "☀️" : "🌙";
      b.title = label;
      b.setAttribute("aria-label", label);
    });
  };
  document.addEventListener("DOMContentLoaded", window.syncThemeButtons);
  document.addEventListener("themechange", window.syncThemeButtons);

  // 用户没显式选过 → 跟随系统变化
  if (prefersDark && prefersDark.addEventListener) {
    prefersDark.addEventListener("change", (e) => {
      if (!localStorage.getItem(KEY)) window.setTheme(e.matches ? "dark" : "light");
    });
  }
})();
