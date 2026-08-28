#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# uv 缓存目录（避免 ~/.cache/uv 下部分目录属主为 root 导致的权限错误）
export UV_CACHE_DIR="${UV_CACHE_DIR:-$(pwd)/.uv-cache}"

# 可选配置文件：~/.config/wallhawk.env 里可放环境变量（PUSHPLUS_TOKEN、ALERT_CHANNEL、
# DATA_SOURCE 等），避免 token 之类敏感信息进入本仓库的 git 历史。
if [ -f "$HOME/.config/wallhawk.env" ]; then
  set -a        # 使文件内的赋值自动 export
  . "$HOME/.config/wallhawk.env"
  set +a
fi

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

echo ">> 启动盯盘助手  http://localhost:${PORT:-8050}"
exec venv/bin/python app.py
