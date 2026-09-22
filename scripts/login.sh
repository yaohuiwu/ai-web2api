#!/usr/bin/env bash
# 一条命令手动登录：在**宿主**打开有头浏览器 → 你完成登录 → 登录态自动导入运行中的服务。
#
#   ./scripts/login.sh                 # 交互选择 provider
#   ./scripts/login.sh chatgpt         # 指定 provider
#   ./scripts/login.sh deepseek --manual
#   AI_WEB2API_URL=http://192.168.1.10:8000 ./scripts/login.sh chatgpt
#
# 说明：
#   - Docker 部署时也**请在宿主执行**（容器内没有可见窗口，无法完成人工登录）。
#   - 只需宿主机装了 Playwright 的运行环境；本脚本会自动挑解释器。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 1) 选一个可用的入口（优先已安装的 console script）
if [ -x ".venv/bin/ai-web2api" ]; then
  CMD=(.venv/bin/ai-web2api)
elif command -v uv >/dev/null 2>&1 && [ -f uv.lock ]; then
  CMD=(uv run ai-web2api)
elif [ -x ".venv/bin/python" ]; then
  CMD=(.venv/bin/python -m ai_web2api.cli)
else
  CMD=(python3 -m ai_web2api.cli)
fi

# 2) 导入地址：显式 --import-url > AI_WEB2API_URL > 由 config 推导（127.0.0.1:<port>）
if [ -z "${AI_WEB2API_URL:-}" ] && [[ " $* " != *" --import-url "* ]]; then
  PORT="$(grep -E '^\s*port:' config.yaml 2>/dev/null | head -1 | grep -oE '[0-9]+' || true)"
  export AI_WEB2API_URL="http://127.0.0.1:${PORT:-8000}"
fi
[ -n "${AI_WEB2API_URL:-}" ] && echo "→ 导入地址：$AI_WEB2API_URL"

echo "→ 即将打开浏览器窗口；完成登录后回终端按【回车】保存（也会尝试自动检测）"
exec "${CMD[@]}" login "$@"
