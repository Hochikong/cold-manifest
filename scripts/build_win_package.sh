#!/usr/bin/env bash
# 构建 Windows 运行包（在 WSL 中执行）：前端 dist + win_amd64/cp310 离线 wheels + 源码 + 脚本 → zip
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(python3 -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])" 2>/dev/null \
  || grep -oP '(?<=^version = ")[^"]+' pyproject.toml)
OUT="dist-packages"
NAME="cold-manifest-${VERSION}-win"
STAGE="${OUT}/${NAME}"

echo "== [1/5] 前端构建 =="
(cd frontend && npm run build)

echo "== [2/5] 解析并下载 Windows 离线 wheels (cp310/win_amd64) =="
rm -rf "$STAGE"
mkdir -p "$STAGE/wheels"
# pip download 的 --platform 不影响环境标记评估（Linux 上会把 uvloop 等 linux-only 依赖解析进来），
# 先用 uv 按 Windows 平台解析出 pin 清单，再逐包 --no-deps 下载对应 wheel。
UV="${UV:-$HOME/.local/bin/uv}"
"$UV" pip compile pyproject.toml --python-platform windows --python-version 3.10 \
  --no-annotate --no-header -o "$OUT/reqs-win.txt"
grep -vE "^cold-manifest" "$OUT/reqs-win.txt" > "$OUT/reqs-win-filtered.txt"
# pip download 需要一个本地 venv 跑 pip；缺失时自动用 uv 创建（--seed 带 pip）
PENV="${PENV:-/tmp/opencode/penv}"
if [ ! -x "$PENV/bin/python" ]; then
  echo "  (venv 缺失，自动创建：$PENV)"
  "$UV" venv --seed "$PENV"
fi
"$PENV/bin/python" -m pip download --no-deps --only-binary=:all: \
  --platform win_amd64 --python-version 310 --implementation cp \
  --dest "$STAGE/wheels" -r "$OUT/reqs-win-filtered.txt"
"$PENV/bin/python" -m pip download --no-deps --only-binary=:all: \
  --platform win_amd64 --python-version 310 --implementation cp \
  --dest "$STAGE/wheels" pip setuptools wheel

echo "== [3/5] 组装包内容 =="
mkdir -p "$STAGE/packaging/windows" "$STAGE/docs" "$STAGE/frontend"
cp -r src "$STAGE/src"
cp pyproject.toml README.md "$STAGE/"
cp -r frontend/dist "$STAGE/frontend/dist"
cp packaging/windows/*.cmd "$STAGE/packaging/windows/"
cp "docs/Windows-运行说明.md" "$STAGE/docs/"
# 清掉开发产物，包内只留干净源码
find "$STAGE/src" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
rm -rf "$STAGE/src/cold_manifest.egg-info" 2>/dev/null || true

echo "== [4/5] 打 zip =="
(cd "$OUT" && rm -f "${NAME}.zip" && python3 -c "import shutil; shutil.make_archive('${NAME}', 'zip', '.', '${NAME}')")
ls -lh "$OUT/${NAME}.zip"

echo "== [5/5] 内容清单 =="
python3 -c "import zipfile;[print(n) for n in zipfile.ZipFile('$OUT/${NAME}.zip').namelist()]" | head -60
echo "..."
python3 -c "import zipfile;[print(n) for n in zipfile.ZipFile('$OUT/${NAME}.zip').namelist()]" | tail -3
echo "完成：$OUT/${NAME}.zip"
