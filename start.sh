#!/usr/bin/env bash
# 超昂大战 角色剧情离线阅读器 · Linux / macOS 启动脚本
set -e
cd "$(dirname "$0")"

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "[x] 没有找到 Python 3。请先安装： https://www.python.org/downloads/"
  exit 1
fi

if ! $PY -c "import PIL" >/dev/null 2>&1; then
  echo "[!] 缺少 Pillow（转 WebP / 超分需要）。安装： $PY -m pip install pillow"
fi

exec $PY -m server --open "$@"
