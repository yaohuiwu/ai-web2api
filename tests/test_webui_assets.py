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



def test_playground_model_switch_unbinds_thread():
    """换模型：自动解绑已绑定会话（否则服务端 409 thread_mismatch）——用户可感知的修复。"""
    js = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")
    assert "let boundThreadModel" in js
    assert "function applyThreadModel" in js
    assert "boundThreadModel" in js
    assert "已改为新对话" in js, "换模型时的提示应说明已改为新对话（并清空上下文）"
    # 切换会话时同步模型 + 提示绑定关系
    assert "switchThread(t.thread_id, t.loaded !== false, t.model)" in js
    assert "该会话绑定模型" in js




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




def test_playground_has_resizable_splitter():
    """中缝可拖拽：左右调整"对话/画面"宽度（双击复位、宽度记忆、窄屏隐藏）。"""
    html = (WEBUI / "playground.html").read_text(encoding="utf-8")
    css = (WEBUI / "assets/css/playground.css").read_text(encoding="utf-8")
    js = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")

    assert 'id="splitHandle"' in html and 'role="separator"' in html
    assert "col-resize" in css and "#splitHandle" in css
    assert "touch-action: none" in css, "触屏也要能拖（pointer events 需禁默认手势）"
    assert "@media (max-width: 900px)" in css and "#splitHandle { display: none !important; }" in css
    # 拖拽/记忆/复位/键盘
    for token in ("pointermove", "pointerup", "setPointerCapture", "aiw2api_live_width",
                  "dblclick", "ArrowLeft", "ArrowRight"):
        assert token in js, f"缺少 {token}"
    # 适应 = 按宽度铺满（消掉黑边）
    # "适应=按宽度铺满（消黑边）"属于组件样式（liveview.css），这里只确认引用关系
    assert "liveview.css" in html
    live_js = (WEBUI / "assets/js/liveview.js").read_text(encoding="utf-8")
    assert 'width = "100%"' in live_js, "组件里适应模式应铺满宽度"


def test_liveview_has_reopen_button():
    """画面组件：常驻「⟳ 重开（回首页）」按钮，用于跳出人机验证/卡死循环页。

    reopen = `page.goto(provider 首页)`，与 reload（原地刷新）区分；
    必须放在头部（不能只藏在交互模式工具条里 —— 出现验证时用户往往还没开交互）。
    """
    js = (WEBUI / "assets/js/liveview.js").read_text(encoding="utf-8")
    assert "⟳ 重开" in js
    assert 'action: "reopen"' in js, "未接线 reopen action"
    assert "head.append(reopenBtn" in js, "reopen 按钮应在头部常驻"


def test_splitter_constants_defined_before_init():
    """守卫：`initLivePane()` 会同步调用分隔条逻辑，常量必须在它之前定义（否则 TDZ 报错）。

    实测踩过：`const LIVE_W_KEY` 追加在文件末尾 → initLivePane() 里同步调用 applyLiveWidth()
    → ReferenceError（Cannot access before initialization）→ 整个画面初始化中断（面板/分隔条都不显示）。
    """
    js = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")
    init_at = js.index("initLivePane();")
    for token in ("const LIVE_W_KEY", "function applyLiveWidth", "function initSplitHandle"):
        at = js.index(token)
        assert at < init_at, f"{token} 必须在 initLivePane() 之前（避免同步初始化时 TDZ）"


def test_splitter_drag_cannot_leak_listeners():
    """拖拽不能污染后续点击：必须处理 pointercancel/失焦/按键已松开，且用完摘掉 window 监听。

    否则"鼠标在窗口外松开"会留下 window 级 pointermove → 之后的普通点击/移动被当成继续拖拽。
    """
    js = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")
    for token in ("pointercancel", '"blur"', "e.buttons === 0", "removeEventListener(\"pointermove\"",
                  "removeEventListener(\"pointerup\"", "setPointerCapture"):
        assert token in js, f"缺少拖拽收尾处理：{token}"



def test_model_switch_resets_conversation():
    """换模型必须换对话：清空消息区 + 内存历史（否则上一家的消息会被带给新 provider）。

    实测事故：在 DeepSeek 界面里出现了 ChatGPT 的报错文本 —— 因为切模型只解绑了 thread_id，
    屏幕上的旧消息（以及内存里的 messages）被继续当作上下文发送。
    """
    js = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")
    assert "function resetConversation(" in js
    assert "currentModel" in js
    seg = js[js.index('$("#model").addEventListener("change"'):]
    seg = seg[:seg.index("});") + 3]
    assert "resetConversation(" in seg, "换模型时必须清空当前对话"
    body = js[js.index("function resetConversation("):]
    body = body[:body.index("\n}")]
    assert "messages = []" in body and '#chat").innerHTML' in body


def test_current_model_initialized_on_load_and_thread_switch():
    """currentModel 必须在"模型列表加载完"和"切换会话"时初始化。

    否则初值 null → 用户第一次换模型时检测不到变化 → 旧 provider 的消息留在屏幕上
    （实测：DeepSeek 界面里出现 ChatGPT 的报错文本）。
    """
    js = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")
    load = js[js.index("async function loadModels"):]
    load = load[:load.index("\n}\n")]
    assert "currentModel = sel.value" in load, "loadModels 结束前要记住当前模型"
    apply_fn = js[js.index("function applyThreadModel"):]
    apply_fn = apply_fn[:apply_fn.index("\n}\n")]
    assert "currentModel = model" in apply_fn, "切会话要同步 currentModel"


def test_pending_reset_on_model_switch_during_stream():
    """生成中换模型：不能半途清屏（会把正在写的回复擦掉）→ 挂起，流结束后再清。"""
    js = (WEBUI / "assets/js/playground.js").read_text(encoding="utf-8")
    assert "pendingReset" in js
    seg = js[js.index('$("#model").addEventListener("change"'):]
    seg = seg[:seg.index("});") + 3]
    assert "if (streaming)" in seg and "pendingReset" in seg
    tail = js[js.index("    streaming = false;"):]
    assert "pendingReset" in tail[:220], "流结束处要执行挂起的 reset"
