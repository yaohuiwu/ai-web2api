"""文本库取题：随机、不重复、可复现，且题目本身够"像人"。

运行：.venv/bin/python -m pytest tests/test_prompt_pool.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompt_pool import CATEGORIES, load_corpus, sample_cases, seed_from_env  # noqa: E402


def test_corpus_is_rich_enough():
    data = load_corpus()
    for tag in CATEGORIES:
        assert tag in data, f"文本库缺少分类 {tag}"
        assert len(data[tag]) >= 8, f"{tag} 题目太少（{len(data[tag])}），容易被看出重复"


def test_corpus_has_no_duplicates_and_looks_human():
    from prompt_pool import SHORT

    data = load_corpus()
    flat = [p for items in data.values() for p in items]
    assert len(flat) == len(set(flat)), "文本库里有重复题目"
    for tag, items in data.items():
        for p in items:
            assert "\n" not in p, f"题目应为单行：{p!r}"
            # short 类（快问快答）**故意**短，其余题目要像真人提问那样有信息量
            if tag != SHORT:
                assert len(p) >= 12, f"题目太短、不像真人提问：{p!r}"


def test_sample_is_random_without_seed():
    """不固定种子 → 多次取样应出现不同题目（否则又是"重复文本"）。"""
    picks = {sample_cases()[0][1] for _ in range(12)}
    assert len(picks) > 1, "取样结果固定不变，等于没用随机"


def test_sample_is_reproducible_with_seed():
    a = sample_cases(seed=42)
    b = sample_cases(seed=42)
    assert a == b, "同一种子应给出同一批题目（便于复现失败）"


def test_sample_has_no_duplicates_within_a_run():
    for _ in range(10):
        cases = sample_cases()
        prompts = [p for _, p in cases]
        assert len(prompts) == len(set(prompts))
        assert [t for t, _ in cases] == list(CATEGORIES)


def test_sample_skips_unknown_category():
    cases = sample_cases(categories=("long", "不存在"))
    assert [t for t, _ in cases] == ["long"]


def test_seed_from_env(monkeypatch):
    monkeypatch.delenv("TEXT_FIDELITY_SEED", raising=False)
    assert seed_from_env() is None
    monkeypatch.setenv("TEXT_FIDELITY_SEED", "7")
    assert seed_from_env() == 7
    monkeypatch.setenv("TEXT_FIDELITY_SEED", "abc")   # 非数字 → 稳定散列，不崩
    assert isinstance(seed_from_env(), int)


def test_corpus_file_is_yaml_readable():
    assert load_corpus()["code"], "代码类题目不能为空"


@pytest.mark.parametrize("tag", CATEGORIES)
def test_each_category_prompt_mentions_expected_shape(tag):
    """分类要名副其实（长文/代码/表格），否则保真度用例断言就没意义。"""
    data = load_corpus()
    hits = 0
    for p in data[tag]:
        if tag == "code" and ("代码块" in p or "Python" in p or "bash" in p or "JavaScript" in p):
            hits += 1
        elif tag == "table" and "表格" in p:
            hits += 1
        elif tag == "long" and "表格" not in p and "代码块" not in p:
            hits += 1
    assert hits >= len(data[tag]) * 0.8, f"{tag} 分类里有太多不符合形态的题目"


def test_short_category_exists_and_is_short():
    """短问答类题目必须真的短（否则 live 快问快答会变慢）。"""
    from prompt_pool import SHORT, sample_one

    data = load_corpus()
    assert SHORT in data, "缺少 short 分类（live 快问快答随机取题用）"
    for p in data[SHORT]:
        assert len(p) <= 20, f"short 分类题目过长：{p!r}"
    assert sample_one(SHORT) in data[SHORT]
    assert sample_one(SHORT, seed=1) == sample_one(SHORT, seed=1)   # 可复现
