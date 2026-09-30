#!/usr/bin/env bash
# cold-manifest Web 服务启动器。默认数据根：<包根>/data
# 用法：packaging/linux/start.sh   （环境变量：CLDM_DATA_ROOT 覆盖数据根；CLDM_PORT 覆盖端口）
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ ! -x ".venv-linux/bin/cldm" ]; then
    echo "错误：未找到 .venv-linux/bin/cldm，请先运行 packaging/linux/setup.sh。" >&2
    exit 1
fi

if [ -z "${CLDM_DATA_ROOT:-}" ]; then
    CLDM_DATA_ROOT="$(pwd)/data"
fi
export CLDM_DATA_ROOT

echo "数据根：$CLDM_DATA_ROOT"
echo "服务地址：http://0.0.0.0:${CLDM_PORT:-8765}（本机：http://localhost:${CLDM_PORT:-8765}，Ctrl+C 停止）"
exec .venv-linux/bin/cldm serve --host 0.0.0.0 --port "${CLDM_PORT:-8765}"
