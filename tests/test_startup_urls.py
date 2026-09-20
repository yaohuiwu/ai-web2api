"""启动时打印的链接必须是真能打开的地址（不能出现 0.0.0.0）。"""

from __future__ import annotations

import logging

from ai_web2api.main import log_ui_urls, usable_hosts


def test_wildcard_host_never_printed() -> None:
    for host in ("0.0.0.0", "::", "*", ""):
        hosts = usable_hosts(host)
        rendered = [h for h, _ in hosts]
        assert "0.0.0.0" not in rendered
        assert "::" not in rendered
        assert rendered[0] == "127.0.0.1"  # 本机永远可用，排第一
        assert [tag for _, tag in hosts][0] == "本机"


def test_wildcard_includes_lan_ip_when_available(monkeypatch) -> None:
    monkeypatch.setattr("ai_web2api.main._lan_ip", lambda: "192.168.1.23")
    assert usable_hosts("0.0.0.0") == [("127.0.0.1", "本机"), ("192.168.1.23", "局域网")]


def test_lan_ip_failure_is_tolerated(monkeypatch) -> None:
    monkeypatch.setattr("ai_web2api.main._lan_ip", lambda: None)
    assert usable_hosts("0.0.0.0") == [("127.0.0.1", "本机")]


def test_explicit_hosts_pass_through() -> None:
    assert usable_hosts("127.0.0.1") == [("127.0.0.1", "本机")]
    assert usable_hosts("192.168.1.5") == [("192.168.1.5", "本机")]
    assert usable_hosts("::1") == [("[::1]", "本机")]  # IPv6 要带方括号才能点


def test_logged_lines_contain_usable_urls(monkeypatch, caplog) -> None:
    monkeypatch.setattr("ai_web2api.main._lan_ip", lambda: "192.168.1.23")
    with caplog.at_level(logging.INFO, logger="ai_web2api"):
        log_ui_urls("0.0.0.0", 8000)
    messages = [r.getMessage() for r in caplog.records]
    text = "\n".join(messages)
    assert "http://127.0.0.1:8000/ui/" in text
    assert "http://127.0.0.1:8000/ui/playground.html" in text
    assert "http://127.0.0.1:8000/v1" in text
    assert "http://192.168.1.23:8000/ui/" in text
    assert "0.0.0.0" not in text


def test_one_url_per_line(monkeypatch, caplog) -> None:
    """一条日志一个地址，别把三个链接挤成一行（终端里会折行，链接就没法点了）。"""
    monkeypatch.setattr("ai_web2api.main._lan_ip", lambda: "192.168.1.23")
    with caplog.at_level(logging.INFO, logger="ai_web2api"):
        log_ui_urls("0.0.0.0", 8000)
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 6  # 2 个入口地址 × 3 条
    for msg in messages:
        assert msg.count("http://") == 1
        assert "http://" in msg and (msg.endswith("/ui/") or msg.endswith(".html") or msg.endswith("/v1"))
        assert len(msg) < 80
    assert messages[0] == "管理界面（本机）：http://127.0.0.1:8000/ui/"
