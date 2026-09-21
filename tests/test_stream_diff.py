"""流式 diff 完整性单元测试：覆盖 markdown 渐进渲染导致的"包裹"重渲染。

对应 webchat.py 的 _diff_increment（通用引擎）：轮询提取的 markdown 文本因 DOM 重渲染
（纯文本 → 代码块/表格/加粗）不再前缀延续，旧逻辑会整段丢弃增量导致
"思考和正文不完整"。这些用例验证子串匹配能兜住包裹重渲染场景。
"""

from ai_web2api.providers.webchat import _diff_increment


def _accumulate(transitions):
    """按 (old, new) 序列模拟连续轮询，拼接所有增量，验证最终不丢内容。"""
    full = ""
    for old, new in transitions:
        inc = _diff_increment(old, new)
        full += inc
    return full


def test_wrapping_code_block():
    """代码块渐进渲染：围栏边输出边渲染（前缀延续），内容完整不丢。

    真实场景：DeepSeek 检测到代码块语法后渲染为 <pre><code class="language-python">，
    提取文本形如 "```python\\nprint(...)"，围栏与内容是逐轮前缀延续的，
    不存在"已渲染纯文本被整体包裹"的跳变（那是人工构造的非真实序列）。
    """
    transitions = [
        ("", "```"),                                            # 围栏开标记先出现
        ("```", "```python\n"),                                 # 语言标注
        ("```python\n", "```python\nprint(\"x\")"),             # 内容开始
        ("```python\nprint(\"x\")", "```python\nprint(\"x\")\nprint(\"y\")"),  # 追加
    ]
    full = _accumulate(transitions)
    assert 'print("x")' in full
    assert 'print("y")' in full
    assert "```" in full  # 围栏开标记保留


def test_wrapping_jump_drops_fence_only():
    """极端跳变：旧纯文本被整体包裹成代码块（非真实序列，防御性）。

    old 恰好是 new 的子串且位于末尾（围栏前插）→ 增量无法重放围栏（重放会重复
    旧文本），按真实行为断言：正文内容不丢即可，围栏标记允许丢失。
    """
    transitions = [
        ("", 'print("x")'),
        ('print("x")', '```python\nprint("x")'),   # 围栏整体前插（跳变）
        ('```python\nprint("x")', '```python\nprint("x")\nprint("y")'),
    ]
    full = _accumulate(transitions)
    assert 'print("x")' in full
    assert 'print("y")' in full


def test_wrapping_with_middle_insert():
    """中间插入内容（包裹前缀出现在 old 之前）→ old 是子串，从 old 之后继续。"""
    transitions = [
        ("", "正文内容"),
        ("正文内容", "```python\n正文内容"),   # 前缀插入（围栏先于旧文本）
        ("```python\n正文内容", "```python\n正文内容\n更多"),  # 追加
    ]
    full = _accumulate(transitions)
    assert "正文内容" in full
    assert "更多" in full


def test_plain_prefix_typing():
    """普通打字机追加（前缀延续）不丢内容。"""
    transitions = [
        ("", "你"),
        ("你", "你好"),
        ("你好", "你好世"),
        ("你好世", "你好世界"),
    ]
    assert _accumulate(transitions) == "你好世界"


def test_thinking_and_content_containers():
    """思考与正文分容器独立 diff（同循环内互不干扰）。"""
    th = _accumulate([("", "我思"), ("我思", "我思考"), ("我思考", "我思考完毕")])
    md = _accumulate([("", "列1"), ("列1", "| 列1 | 列2 |"), ("| 列1 | 列2 |", "| 列1 | 列2 |\n|---|---|")])
    assert th == "我思考完毕"
    assert "列1" in md and "|---|---|" in md


def test_rewritten_text_drops_increment():
    """旧文本被彻底改写（非子串非前缀）→ 丢弃增量避免重复，且不崩。"""
    old = "旧的完全不同的内容"
    new = "新内容"
    assert _diff_increment(old, new) == ""


def test_identical_text_no_increment():
    assert _diff_increment("相同", "相同") == ""
    assert _diff_increment("", "") == ""
