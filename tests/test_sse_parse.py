"""_parse_sse_snapshot 单测：从 DeepSeek SSE 快照解析 thinking/content（含搜索资料）。

样本结构来自真实抓包（/api/v0/chat/completion 的 message 事件，v.response.fragments）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ai_web2api.providers.deepseek import DeepSeekProvider  # noqa: E402

parse = DeepSeekProvider._parse_sse_snapshot


def _snapshot(fragments, status="WIP"):
    import json

    msg = {"v": {"response": {"message_id": 2, "status": status, "fragments": fragments}}}
    return (
        "event: ready\ndata: {\"request_message_id\":1}\n\n"
        "event: message\ndata: " + json.dumps(msg, ensure_ascii=False) + "\n\n"
        "event: close\ndata: {\"click_behavior\":\"none\"}\n\n"
    )


THINK1 = {"id": 2, "type": "THINK", "content": "用户想知道2024年新能源汽车销量。", "stage_id": 1}
TOOL_SEARCH = {
    "id": 3, "type": "TOOL_SEARCH", "status": "DONE", "content": None,
    "queries": [{"query": "2024年 中国 新能源汽车 销量"}],
    "results": [], "stage_id": 1,
}
TOOL_OPEN = {
    "id": 5, "type": "TOOL_OPEN", "status": "DONE",
    "result": {
        "url": "https://www.gov.cn/yaowen/shipin/202501/content_6998312.htm",
        "title": "2024年中国汽车产销量再创新高",
        "snippet": "最新数据同时显示，2024年，我国新能源汽车产销量分别完成1288.8万辆和1286.6万辆。",
        "cite_index": None, "site_name": "中国政府网", "query_indexes": [0],
    },
    "reference": {"id": 3, "type": "TOOL_SEARCH"}, "stage_id": 1,
}
THINK2 = {"id": 9, "type": "THINK", "content": "这些来源都提供了官方数据。", "stage_id": 3}
RESPONSE = {"id": 10, "type": "RESPONSE", "content": "根据中汽协数据，2024年销量1286.6万辆[reference:0]。", "stage_id": 3}


def test_empty():
    assert parse("") == ("", "")
    assert parse(None) == ("", "")


def test_with_search_sources():
    """搜索模式：thinking 含思考 + 搜索资料块（结构化）。"""
    sse = _snapshot([THINK1, TOOL_SEARCH, TOOL_OPEN, THINK2, RESPONSE])
    thinking, content = parse(sse)
    assert "用户想知道2024年新能源汽车销量。" in thinking
    assert "这些来源都提供了官方数据。" in thinking
    # 搜索资料块
    assert "【搜索资料】" in thinking
    assert "1. 2024年中国汽车产销量再创新高（中国政府网）" in thinking
    assert "https://www.gov.cn/yaowen/shipin/202501/content_6998312.htm" in thinking
    assert "1288.8万辆和1286.6万辆" in thinking
    # 正文
    assert "根据中汽协数据，2024年销量1286.6万辆[reference:0]。" in content


def test_no_search():
    """无搜索：thinking 只有思考，无【搜索资料】块。"""
    sse = _snapshot([THINK1, RESPONSE])
    thinking, content = parse(sse)
    assert "用户想知道2024年新能源汽车销量。" in thinking
    assert "【搜索资料】" not in thinking
    assert "根据中汽协数据" in content


def test_dedupe_same_url():
    """同一 URL 的搜索结果去重。"""
    sse = _snapshot([THINK1, TOOL_OPEN, TOOL_OPEN])
    thinking, _ = parse(sse)
    assert thinking.count("2024年中国汽车产销量再创新高") == 1


def test_thinking_empty_when_no_think():
    """只有正文没有思考（快速模式）：thinking 为空，content 正常。"""
    sse = _snapshot([RESPONSE])
    thinking, content = parse(sse)
    assert thinking == ""
    assert "根据中汽协数据" in content


def test_incremental_patch_stream():
    """真实形态：首帧完整快照 + 后续 content APPEND 增量 patch，需重建完整状态。"""
    import json

    first = {"v": {"response": {"message_id": 2, "status": "WIP", "fragments": [
        {"id": 2, "type": "THINK", "content": "用户想知道。", "stage_id": 1},
    ]}}}
    inc_think = {"p": "response/fragments/-1", "o": "BATCH", "v": [
        {"p": "content", "o": "APPEND", "v": "需要查一下资料。"},
        {"p": "content", "o": "APPEND", "v": "查到后再回答。"},
    ]}
    inc_tool = {"p": "response/fragments", "o": "APPEND", "v": [
        {"id": 5, "type": "TOOL_OPEN",
         "result": {"url": "https://example.com/a", "title": "示例来源",
                    "snippet": "这是摘要。", "site_name": "示例站"}},
    ]}
    inc_resp = [
        {"p": "response/fragments", "o": "APPEND", "v": [
            {"id": 10, "type": "RESPONSE", "content": "答案", "stage_id": 3},
        ]},
        {"p": "response/fragments/-1", "o": "BATCH", "v": [
            {"p": "content", "o": "APPEND", "v": "是 42。"},
        ]},
    ]
    sse = (
        "event: message\ndata: " + json.dumps(first, ensure_ascii=False) + "\n\n"
        "event: message\ndata: " + json.dumps(inc_think, ensure_ascii=False) + "\n\n"
        "event: message\ndata: " + json.dumps(inc_tool, ensure_ascii=False) + "\n\n"
        "event: message\ndata: " + json.dumps(inc_resp[0], ensure_ascii=False) + "\n\n"
        "event: message\ndata: " + json.dumps(inc_resp[1], ensure_ascii=False) + "\n\n"
        "event: close\ndata: {}\n\n"
    )
    thinking, content = parse(sse)
    assert thinking == "用户想知道。需要查一下资料。查到后再回答。\n\n【搜索资料】\n1. 示例来源（示例站）\n   https://example.com/a\n   这是摘要。"
    assert content == "答案是 42。"


def test_implicit_append_without_o():
    """DeepSeek 实测：content 增量操作常省略 o 字段（默认追加）。"""
    import json

    first = {"v": {"response": {"message_id": 2, "status": "WIP", "fragments": [
        {"id": 10, "type": "RESPONSE", "content": "根据", "stage_id": 3},
    ]}}}
    no_o = {"p": "response/fragments/-1/content", "v": "中汽协数据，"}
    no_o2 = {"p": "response/fragments/-1", "o": "BATCH", "v": [
        {"p": "content", "v": "2024年销量1286.6万辆"},
    ]}
    sse = (
        "event: message\ndata: " + json.dumps(first, ensure_ascii=False) + "\n\n"
        "event: message\ndata: " + json.dumps(no_o, ensure_ascii=False) + "\n\n"
        "event: message\ndata: " + json.dumps(no_o2, ensure_ascii=False) + "\n\n"
        "event: close\ndata: {}\n\n"
    )
    thinking, content = parse(sse)
    assert content == "根据中汽协数据，2024年销量1286.6万辆"


def test_pathless_string_increments():
    """DeepSeek 实测：正文/思考的逐字增量是 {"v":"文本"}（无 p/o 字段），
    追加到当前（最后一个）fragment。这是正文主体真正的传输通道。"""
    import json

    first = {"v": {"response": {"message_id": 2, "status": "WIP", "fragments": [
        {"id": 2, "type": "THINK", "content": "用户", "stage_id": 1},
    ]}}}
    events = [
        {"v": "需要"},
        {"v": "查一下。"},
        {"p": "response/fragments", "o": "APPEND", "v": [
            {"id": 10, "type": "RESPONSE", "content": "根据", "stage_id": 3},
        ]},
        {"v": "中汽协数据，"},
        {"v": "2024年销量"},
        {"v": "1286.6万辆"},
        {"p": "response/fragments/-1/content", "o": "APPEND", "v": "[reference:0]"},
        {"v": "。"},
    ]
    sse = (
        "event: message\ndata: " + json.dumps(first, ensure_ascii=False) + "\n\n"
        + "".join(
            "event: message\ndata: " + json.dumps(e, ensure_ascii=False) + "\n\n"
            for e in events
        )
        + "event: close\ndata: {}\n\n"
    )
    thinking, content = parse(sse)
    assert thinking == "用户需要查一下。"
    assert content == "根据中汽协数据，2024年销量1286.6万辆[reference:0]。"
