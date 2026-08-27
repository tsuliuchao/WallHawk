#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# uv 缓存目录（避免 ~/.cache/uv 下部分目录属主为 root 导致的权限错误）
export UV_CACHE_DIR="${UV_CACHE_DIR:-$(pwd)/.uv-cache}"

# 虚拟环境
if [ ! -d venv ]; then
  echo ">> 创建虚拟环境 venv (uv)"
  uv venv venv
fi

echo ">> 安装依赖 (uv)"
uv pip install --python venv/bin/python -r requirements.txt

# 可选：启用富途数据源（需先安装 futu-api 并启动 OpenD）
#   uv pip install --python venv/bin/python futu-api
#   export DATA_SOURCE=FUTU
#   ./run.sh

echo ">> 启动盯盘面板  http://localhost:${PORT:-8050}"
exec venv/bin/python app.py
