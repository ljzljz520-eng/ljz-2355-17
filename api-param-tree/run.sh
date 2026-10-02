#!/usr/bin/env bash
# 一键：建库 + 种子（v1 -> 示例 -> v2）+ 启动
set -e
cd "$(dirname "$0")"
PORT="${1:-8000}"
python3 seed.py --reset
exec python3 -m schema_service.server "$PORT"
