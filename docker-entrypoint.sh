#!/usr/bin/env sh
# 可选：WEB2API_HEADLESS=false 时用 Xvfb 跑 headful Chromium。
# ChatGPT（Sentinel 反爬）等站点会拦 headless；headful（哪怕在 Xvfb 里）可通过。
# 默认（未设置/false 之外）保持 headless，行为与镜像原本一致。
#
# 不用 xvfb-run：它在本镜像里会卡在“等 X 就绪”。手动起 Xvfb 更确定。
set -e

case "${WEB2API_HEADLESS:-}" in
  false|False|0|no|off)
    echo "[entrypoint] WEB2API_HEADLESS=${WEB2API_HEADLESS} → headful under Xvfb (DISPLAY=:99)"
    Xvfb :99 -screen 0 1440x900x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
    export DISPLAY=:99
    i=0
    while [ "$i" -lt 50 ]; do
      [ -e /tmp/.X11-unix/X99 ] && break
      sleep 0.2
      i=$((i + 1))
    done
    exec "$@"
    ;;
  *)
    echo "[entrypoint] headless 模式（未启用 Xvfb）"
    exec "$@"
    ;;
esac
