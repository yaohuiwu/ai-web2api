"""webui 结构约束：HTML / CSS / JS 分离，且引用的资源都存在。

运行：.venv/bin/python -m pytest tests/test_webui_assets.py -v
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEBUI = Path(__file__).resolve().parent.parent / "src" / "ai_web2api" / "webui"
HTML_FILES = ("index.html", "playground.html", "threads.html", "browser.html")


def test_no_inline_style_or_script():
    """HTML 只负责结构：不允许内联 <style> 或带内容的 <script>。"""
    for name in HTML_FILES:
        s = (WEBUI / name).read_text(encoding="utf-8")
        assert "<style>" not in s, f"{name} 仍有内联 <style>"
        # 带内容的内联 <script>（有 src 的 <script src=...></script> 允许）
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", s), f"{name} 仍有内联 <script>"


def test_referenced_assets_exist():
    for name in HTML_FILES:
        s = (WEBUI / name).read_text(encoding="utf-8")
        refs = re.findall(r'(?:href|src)="(assets/[^"]+)"', s)
        assert refs, f"{name} 未引用任何 assets 资源"
        for ref in refs:
            assert (WEBUI / ref).is_file(), f"{name} 引用的 {ref} 不存在"


def test_shared_assets_present():
    for rel in ("assets/css/base.css", "assets/js/common.js"):
        assert (WEBUI / rel).is_file(), f"缺少共享资源 {rel}"


def test_markdown_renders_images_and_autolinks():
    js = (WEBUI / "assets/js/markdown.js").read_text(encoding="utf-8")
    assert 'class="md-img"' in js, "markdown 未渲染图片（![](url) → <img>）"
    assert "referrerpolicy" in js, "图片未带 referrerpolicy（外站图可能 403）"
    assert "裸 URL 自动链接" in js, "未把裸 URL 自动变成链接（DeepSeek 思考引用）"


_JS_PROBE = r"""
global.esc = (s) => String(s ?? "");
eval(require("fs").readFileSync(process.argv[2], "utf8"));
const U = "https://images.example.com/a_b_c/d_e/f?purpose=fullsize";
const out = md("![](" + U + ")");
if (!out.includes('src="' + U + '"')) throw new Error("image URL corrupted: " + out);
if (out.includes("<em>")) throw new Error("emphasis leaked into URL: " + out);
if (!out.includes('target="_blank"')) throw new Error("attr corrupted: " + out);
const link = md("见 https://example.com/a_b_c 与 `https://x_y_z`");
if (!link.includes('href="https://example.com/a_b_c"')) throw new Error("autolink broken: " + link);
if (!link.includes("<code>https://x_y_z</code>")) throw new Error("code should not link: " + link);
console.log("ok");
"""


def test_markdown_does_not_corrupt_urls(tmp_path):
    """回归：URL 里的下划线不能被斜体规则误伤（图片 src 被拆坏会显示不出来）。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("node 不可用")
    script = tmp_path / "probe.js"
    script.write_text(_JS_PROBE, encoding="utf-8")
    subprocess.run(
        [node, str(script), str(WEBUI / "assets/js/markdown.js")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_playground_shows_waiting_indicator():
    """等待响应时要有 UI 提示（动点 + 秒数），避免看起来卡死。"""
    js = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")
    css = (WEBUI / "assets/css/playground.css").read_text(encoding="utf-8")
    assert "function showPending" in js
    assert "等待响应…" in js
    assert "stopPending" in js
    # 非流式也要显示（整包等待最容易以为卡死）
    assert "sendNonStream" in js and "showPending(pendingDiv)" in js
    assert ".pending" in css and "pending-bounce" in css


def test_no_hardcoded_colors_outside_tokens():
    """色值必须集中在 tokens.css —— 其它 CSS 只能用变量（保证暗色主题不漏）。"""
    for f in sorted((WEBUI / "assets/css").glob("*.css")):
        if f.name == "tokens.css":
            continue
        body = f.read_text(encoding="utf-8")
        found = re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
        assert not found, f"{f.name} 仍有硬编码色值：{found[:5]}"


def test_theme_tokens_and_toggle():
    assert (WEBUI / "assets/css/tokens.css").is_file()
    assert (WEBUI / "assets/js/theme.js").is_file()
    css = (WEBUI / "assets/css/tokens.css").read_text(encoding="utf-8")
    assert '[data-theme="dark"]' in css, "缺少暗色主题变量"
    for name in HTML_FILES:
        html = (WEBUI / name).read_text(encoding="utf-8")
        assert "assets/css/tokens.css" in html, f"{name} 未引入 tokens.css"
        assert "assets/js/theme.js" in html, f"{name} 未引入 theme.js"
        assert "data-theme-toggle" in html, f"{name} 缺少主题切换按钮"


def test_shared_header_footer_and_meta():
    """两页共享 header(nav) / footer / favicon / meta description。"""
    for name in HTML_FILES:
        html = (WEBUI / name).read_text(encoding="utf-8")
        assert 'class="nav"' in html, f"{name} 缺少导航"
        assert 'data-footer' in html, f"{name} 缺少 footer 占位"
        assert 'rel="icon"' in html, f"{name} 缺少 favicon"
        assert 'name="description"' in html, f"{name} 缺少 meta description"
        assert 'name="color-scheme"' in html, f"{name} 缺少 color-scheme"
    base = (WEBUI / "assets/css/base.css").read_text(encoding="utf-8")
    assert ".nav a.active" in base and "footer .f-links" in base
    common = (WEBUI / "assets/js/common.js").read_text(encoding="utf-8")
    assert "function renderFooter" in common and "function toast" in common and "function copyText" in common


def test_status_panel_polish():
    js = (WEBUI / "assets/js/index.js").read_text(encoding="utf-8")
    assert "function dotClass" in js, "provider 标签状态点未按认证状态着色"
    assert "auth-bar" in js, "缺少认证有效期进度条"
    assert "pv-summary" in js, "缺少 provider 摘要行"
    css = (WEBUI / "assets/css/index.css").read_text(encoding="utf-8")
    assert ".auth-bar" in css and ".kpi-top" in css and ".mini-bar" in css
    # 会话浏览已拆到独立页：这里只留摘要 + 入口（表格与逐条操作见 threads.js）
    assert "renderThreadSummary" in js and "threads.html" in js
    assert "renderThreads(" not in js, "状态面板不应再渲染会话表格"
    pg = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")
    assert "thread_id" in pg and "URLSearchParams" in pg, "Playground 未支持 ?thread_id="


def test_playground_polish():
    js = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")
    html = (WEBUI / "playground.html").read_text(encoding="utf-8")
    css = (WEBUI / "assets/css/playground.css").read_text(encoding="utf-8")
    assert "function relTime" in js, "缺少相对时间"
    assert "msg-foot" in js and "footTiming" in js, "消息缺少角色/时间/耗时信息行"
    assert "wireMsgActions" in js and "copy-json" in js, "缺少复制/复制 JSON"
    assert "toggleRawPanel" in js and "function curlFor" in js, "缺少原始报文面板/curl 导出"
    assert "threadSearch" in html and "#threadSearch" in js, "缺少会话搜索"
    assert "admin/threads?" in js, "侧栏搜索应走服务端（否则超出上限搜不到）"
    assert "setTimeout(refreshThreads, 300)" in js, "侧栏搜索未防抖"
    assert "会话页" in js, "侧栏未提供会话页入口"
    assert "advToggle" in html and ".toolbar-toggle" in css, "缺少窄屏高级项折叠"
    assert "msg-foot" in css and "#rawPanel" in css


def test_quickstart_panel():
    """快速接入：curl / Python / Node 片段 + 复制（开源项目标配）。"""
    html = (WEBUI / "index.html").read_text(encoding="utf-8")
    js = (WEBUI / "assets/js/index.js").read_text(encoding="utf-8")
    css = (WEBUI / "assets/css/index.css").read_text(encoding="utf-8")
    assert 'id="quickstart"' in html
    assert "function qsSnippets" in js and "renderQuickstart" in js
    for k in ("curl", "python", "node"):
        assert k in js
    assert "location.origin" in js, "片段未按访问来源生成 base_url"
    assert "chat.completions.create" in js, "缺少 OpenAI SDK 片段"
    assert ".qs-pre" in css and ".qs-tab" in css


def test_login_guidance_uses_one_command_with_import_url():
    """UI 登录引导：一条命令 + 自动带上当前服务地址（不分本机/Docker）。"""
    js = (WEBUI / "assets/js/index.js").read_text(encoding="utf-8")
    assert "./scripts/login.sh" in js, "未引导使用便捷脚本"
    assert "ai-web2api login" in js, "未给出短命令等价写法"
    assert "--import-url ${location.origin}" in js, "命令未自动带上当前服务地址"
    assert "function copyText" not in js, "copyText 应只在 common.js 定义"
    assert "loginStamp" in js, "导入面板未显示最近登录时间"


def test_threads_page_features():
    """会话独立页：搜索 / provider 过滤 / 每页条数 / 加载更多 / 续用。"""
    html = (WEBUI / "threads.html").read_text(encoding="utf-8")
    js = (WEBUI / "assets/js/threads.js").read_text(encoding="utf-8")
    css = (WEBUI / "assets/css/threads.css").read_text(encoding="utf-8")
    for el in ("q", "provider", "limit", "moreBtn", "list", "autoRefresh"):
        assert f'id="{el}"' in html, f"threads.html 缺少 #{el}"
    assert "admin/threads?" in js, "未按查询参数请求 /admin/threads"
    assert "loadMore" in js and "has_more" in js, "缺少分页/加载更多"
    assert "setTimeout(() => loadMore(true), 300)" in js, "搜索未做防抖"
    assert "playground.html?thread_id=" in js, "缺少续用入口"
    assert "data-kill" in js, "缺少销毁"
    assert "没有匹配的会话" in js, "缺少无结果空状态"
    assert ".tcard" in css and "text-overflow: ellipsis" in css, "标题未截断"
    # 三个页面导航互链
    for name in ("index.html", "playground.html", "threads.html"):
        assert "/ui/threads.html" in (WEBUI / name).read_text(encoding="utf-8")


def test_github_star_badge():
    """UI 顶部：GitHub 仓库入口 + Star 星标数（后端带缓存拉取，失败降级为 Star 文案）。"""
    common = (WEBUI / "assets/js/common.js").read_text(encoding="utf-8")
    assert 'const REPO_URL = "https://github.com/yaohuiwu/ai-web2api"' in common
    assert "function renderRepoBadge" in common and 'fetch("/admin/repo")' in common
    assert 'textContent = "Star"' in common, "拉取失败时应降级显示 Star"
    for name in HTML_FILES:
        html = (WEBUI / name).read_text(encoding="utf-8")
        assert "data-gh-star" in html, f"{name} 缺少 Star 徽章"
        assert "github.com/yaohuiwu/ai-web2api" in html, f"{name} 缺少仓库链接"
    base = (WEBUI / "assets/css/base.css").read_text(encoding="utf-8")
    assert ".gh-star" in base
    tokens = (WEBUI / "assets/css/tokens.css").read_text(encoding="utf-8")
    assert "--star:" in tokens, "星标颜色未进 tokens（硬编码色值会破坏暗色主题）"


def test_live_view_page():
    """实时画面页（只读直播）：MJPEG <img> + 暂停 + 保存当前帧 + 未鉴权提示。"""
    html = (WEBUI / "browser.html").read_text(encoding="utf-8")
    js = (WEBUI / "assets/js/browser.js").read_text(encoding="utf-8")
    css = (WEBUI / "assets/css/browser.css").read_text(encoding="utf-8")
    for el in ("provider", "fps", "quality", "pause", "save", "live", "status"):
        assert f'id="{el}"' in html, f"browser.html 缺少 #{el}"
    assert "stream.mjpg" in js and "screen.jpg" in js and "screen/state" in js
    assert "live-warn" in html and "请勿对外暴露" in html, "必须提示未鉴权风险"
    assert ".screen-wrap" in css
    # 其它页导航与状态面板入口
    for name in HTML_FILES:
        assert "/ui/browser.html" in (WEBUI / name).read_text(encoding="utf-8"), name
    idx = (WEBUI / "assets/js/index.js").read_text(encoding="utf-8")
    assert "/ui/browser.html?provider=" in idx, "provider 详情缺「查看画面」入口"


def test_playground_model_switch_unbinds_thread():
    """换模型：自动解绑已绑定会话（否则服务端 409 thread_mismatch）——用户可感知的修复。"""
    js = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")
    assert "let boundThreadModel" in js
    assert "function applyThreadModel" in js
    assert 'boundThreadModel' in js and '已自动改为新会话' in js
    # 切换会话时同步模型 + 提示绑定关系
    assert "switchThread(t.thread_id, t.loaded !== false, t.model)" in js
    assert "该会话绑定模型" in js


def test_live_view_follows_active_session():
    """画面必须能看"你正在聊的那个会话"：MRU 选页 + ?thread_id= 固定 + 显示来源。"""
    js = (WEBUI / "assets/js/browser.js").read_text(encoding="utf-8")
    assert "pinnedThread" in js and "thread_id" in js, "未支持 ?thread_id= 固定会话"
    assert "shown_thread_id" in js, "未显示画面来自哪个会话"
    assert "pinnedThread = null" in js, "切换 provider 时应解除固定"
    tj = (WEBUI / "assets/js/threads.js").read_text(encoding="utf-8")
    assert "browser.html?provider=" in tj and "thread_id=" in tj, "会话页缺「画面」入口"


def test_live_view_dismiss_button():
    """画面页可手工关闭弹窗（弹窗遮住输入时用）。"""
    html = (WEBUI / "browser.html").read_text(encoding="utf-8")
    js = (WEBUI / "assets/js/browser.js").read_text(encoding="utf-8")
    assert 'id="dismiss"' in html
    assert "/dismiss" in js and "关闭弹窗" in html


def test_playground_widget_panel():
    """交互组件面板：沙箱 iframe（不给 allow-same-origin）+ 截图切换 + 历史回填。"""
    js = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")
    css = (WEBUI / "assets/css/playground.css").read_text(encoding="utf-8")
    assert "function widgetsHtml" in js and "function wireWidgets" in js
    assert 'sandbox="allow-scripts"' in js, "组件必须沙箱运行"
    assert "allow-same-origin" not in js, "绝不能给组件同源权限"
    assert "widget-frame" in js and "widget-shot" in js and "看截图" in js
    assert "m.widgets" in js and "obj.widgets" in js, "历史/流式都要回填组件"
    assert ".widget-box" in css


def test_playground_has_split_live_pane():
    """Playground 左右分栏：左侧原消息区 + 右侧实时画面（便于边聊边对）。"""
    html = (WEBUI / "playground.html").read_text(encoding="utf-8")
    css = (WEBUI / "assets/css/playground.css").read_text(encoding="utf-8")
    js = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")

    assert 'id="split"' in html and '<aside id="livePane"' in html, "缺少分栏容器/画面面板"
    assert 'id="live"' in html and 'id="liveToggle"' in html
    # 画面面板必须在 #chat 之后（左对话、右画面）
    assert html.index('id="chat"') < html.index('id="livePane"')
    assert "#split { display: flex" in css or "#split { flex: 1; display: flex" in css
    assert "@media (max-width: 900px)" in css, "窄屏应上下分栏"
    # 复用画面页的直播接口；thread_id 固定 + 只在可见时开流
    assert "/stream.mjpg" in js and "screen/state" in js
    assert "thread_id=" in js and "visibilitychange" in js
    assert "/dismiss" in js, "应能关闭站点遮挡弹窗"
    # 模型 → provider 映射（画面要跟着所选模型切）
    assert "/admin/status" in js and "PROVIDER_MAP" in js
