#!/usr/bin/env bash
# cold-manifest CLI 包装器。示例：cldm.sh collect /media/usb1 --serial XXX
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ ! -x ".venv-linux/bin/cldm" ]; then
    echo "错误：未找到 .venv-linux/bin/cldm，请先运行 packaging/linux/setup.sh。" >&2
    exit 1
fi

# 默认数据根：<包根>/data（显式 --data-root 参数优先）
if [ -z "${CLDM_DATA_ROOT:-}" ]; then
    CLDM_DATA_ROOT="$(pwd)/data"
fi
export CLDM_DATA_ROOT

exec .venv-linux/bin/cldm "$@"
