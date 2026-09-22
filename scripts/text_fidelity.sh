#!/usr/bin/env bash
# 文本捕获保真度回归（日常跑）：断言"我们返回的正文 == 页面可见文本"。
#   ./scripts/text_fidelity.sh              # 全部 provider（Kimi 排队时自动 skip）
#   ./scripts/text_fidelity.sh deepseek     # 只跑一个 provider
# 退出码 = pytest 结果（非 0 即有用例不达标）→ 可直接给 cron/监控告警。
# 详见 docs/TEXT_FIDELITY.md
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY=""
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"
elif command -v uv >/dev/null 2>&1 && [ -f uv.lock ]; then PY="uv run python"
else PY="python3"; fi

STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p reports
LOG="reports/text_fidelity-${STAMP}.log"
FILTER="${1:-}"
ARGS=(-m live tests/test_text_fidelity.py -v)
[ -n "$FILTER" ] && ARGS+=(-k "$FILTER")

LABEL="全部 provider"
[ -n "$FILTER" ] && LABEL="provider=$FILTER"
echo "→ 跑文本保真度回归（${LABEL}）；日志：${LOG}"
set +e
$PY -m pytest "${ARGS[@]}" 2>&1 | tee "$LOG"
CODE=${PIPESTATUS[0]}
set -e

if [ -f reports/text_fidelity.jsonl ]; then
  echo "→ 本次结果（JSONL 末尾 12 行）："
  tail -n 12 reports/text_fidelity.jsonl
fi
if [ "$CODE" -ne 0 ]; then
  echo "⚠ 有用例不达标（覆盖率 < 0.95 或正文混入思考特征文本）—— 见 ${LOG}"
else
  echo "✅ 全部达标（或未完成的 provider 已 skip）"
fi
exit "$CODE"
