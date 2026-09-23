"""本地文本库取题：live 测试与探针**随机取样**，不再反复用同一句话。

背景（实测教训）：固定、重复的文本是自动化特征。反复用同一句 prompt 打豆包，
会被弹人机验证卡片，请求直接失败。所以：
  - 题目从 ``tests/prompts/corpus.yaml`` 里随机取（同一轮内不重复）；
  - 需要复现时用 ``TEXT_FIDELITY_SEED`` 固定随机种子；
  - 本轮实际用了哪些题会写进报告（``reports/text_fidelity.jsonl``）。

用法::

    from prompt_pool import sample_cases
    CASES = sample_cases()               # [("long", "…"), ("code", "…"), ("table", "…")]
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import yaml

CORPUS = Path(__file__).resolve().parent / "prompts" / "corpus.yaml"
CATEGORIES = ("long", "code", "table")
SHORT = "short"          # 极短回复类（"快问快答"用例用，同样随机取）


def load_corpus(path: Path | str = CORPUS) -> dict[str, list[str]]:
    """读文本库；返回 ``{分类: [题目, …]}``（空分类会被丢掉）。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: dict[str, list[str]] = {}
    for key, items in raw.items():
        prompts = [str(x).strip() for x in (items or []) if str(x).strip()]
        if prompts:
            out[str(key)] = prompts
    if not out:
        raise ValueError(f"文本库为空：{path}")
    return out


def seed_from_env() -> int | None:
    """``TEXT_FIDELITY_SEED``（可复现）；未设置 → ``None``（真随机）。"""
    raw = os.environ.get("TEXT_FIDELITY_SEED", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return abs(hash(raw)) % (2**31)


def sample_cases(
    categories: list[str] | tuple[str, ...] = CATEGORIES,
    seed: int | None = None,
    corpus: dict[str, list[str]] | None = None,
) -> list[tuple[str, str]]:
    """每个分类随机取**一条**题目，返回 ``[(分类, 题目), …]``。

    同一轮内不重复；``seed`` 固定时可复现（便于排查失败）。
    """
    data = corpus or load_corpus()
    rng = random.Random(seed)
    used: list[str] = []
    cases: list[tuple[str, str]] = []
    for tag in categories:
        pool = [p for p in data.get(tag, []) if p not in used]
        if not pool:                      # 该分类没配题目 → 跳过而不是报错
            continue
        prompt = rng.choice(pool)
        used.append(prompt)
        cases.append((tag, prompt))
    if not cases:
        raise ValueError(f"没有可用题目：categories={list(categories)}")
    return cases


def sample_one(category: str, seed: int | None = None, corpus: dict[str, list[str]] | None = None) -> str:
    """随机取**一条**指定分类的题目（如 ``short``）。"""
    data = corpus or load_corpus()
    pool = data.get(category) or []
    if not pool:
        raise ValueError(f"文本库分类为空：{category}")
    return random.Random(seed).choice(pool)


if __name__ == "__main__":  # 临时探针取题：python tests/prompt_pool.py [分类]
    import sys as _sys

    _cat = _sys.argv[1] if len(_sys.argv) > 1 else "long"
    print(sample_one(_cat, seed=seed_from_env()))
