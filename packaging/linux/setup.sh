#!/usr/bin/env bash
# cold-manifest Linux 离线/在线安装：创建 venv 并安装本包
# 用法：packaging/linux/setup.sh
# 前提：python3 >= 3.10；包根若带 wheels-linux/ 则完全离线安装
set -euo pipefail
cd "$(dirname "$0")/../.."

# [1/3] 检查 python3 >= 3.10
if ! command -v python3 >/dev/null 2>&1; then
    echo "错误：未找到 python3，请先安装 Python 3.10 或更高版本（如 sudo apt install python3 python3-venv）。" >&2
    exit 1
fi
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "错误：Python 版本过旧（$(python3 -V 2>&1)），需要 >= 3.10。" >&2
    exit 1
fi
echo "Python 版本：$(python3 -V) ✓"

echo "[1/3] 创建 venv .venv-linux ..."
python3 -m venv .venv-linux || {
    echo "错误：创建 venv 失败。请确认已安装 python3-venv（如 sudo apt install python3-venv）。" >&2
    exit 1
}

PY=".venv-linux/bin/python"
echo "[2/3] 升级 pip ..."
"$PY" -m pip install --upgrade pip

# [3/3] 安装：优先离线 wheels-linux/，否则在线
if [ -d wheels-linux ] && ls wheels-linux/*.whl >/dev/null 2>&1; then
    echo "[3/3] 离线安装（模式：wheels-linux/ 离线）..."
    "$PY" -m pip install --no-index --find-links wheels-linux -e .
else
    echo "[3/3] 在线安装（模式：网络在线，pip install -e .）..."
    "$PY" -m pip install -e .
fi

"$PY" -m cold_manifest.cli --version 2>/dev/null || .venv-linux/bin/cldm --version
echo "安装完成。运行 packaging/linux/start.sh 启动 Web 服务。"
