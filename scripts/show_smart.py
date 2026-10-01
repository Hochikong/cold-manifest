"""打印某个快照里记录的 SMART / 探测结果（诊断用，只读）。

用途：Windows 上"采集不到 SMART"时，看快照 meta 里到底记了什么原因
（`smart_error` 人话 + `smart_error_raw` 原始 stderr 片段 + `smart_raw_json` 成功时的原文）。

用法（在包根，或任意目录）：
    .venv-win\\Scripts\\python.exe show_smart.py
    .venv-win\\Scripts\\python.exe show_smart.py "20260123004775F_P2/20261001T151548Z"
    .venv-win\\Scripts\\python.exe show_smart.py <snapshot.db 路径>

不传参数时，扫描默认数据根，挑**最新**的一个快照来打印。
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

DEFAULT_DATA_ROOT = pathlib.Path(r"D:\Vibe软件\cold-manifest-0.2.0-win\data")


def find_snapshot(arg: str | None) -> pathlib.Path:
    if arg:
        p = pathlib.Path(arg)
        if p.is_file():
            return p
        cand = DEFAULT_DATA_ROOT / arg / "snapshot.db"
        if cand.is_file():
            return cand
        raise SystemExit(f"找不到快照：{arg}（也试过 {cand}）")
    dbs = sorted(DEFAULT_DATA_ROOT.glob("*/*/snapshot.db"), key=lambda p: p.stat().st_mtime)
    if not dbs:
        raise SystemExit(f"{DEFAULT_DATA_ROOT} 下没有找到任何快照库")
    return dbs[-1]


def main() -> int:
    db = find_snapshot(sys.argv[1] if len(sys.argv) > 1 else None)
    print(f"快照库：{db}")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    keys = [r[0] for r in conn.execute(
        "SELECT key FROM meta WHERE key LIKE 'smart%' OR key LIKE 'probe%' ORDER BY key")]
    if not keys:
        print("meta 里没有任何 smart* / probe* 记录：当时未尝试读 SMART，或快照是旧版本采集的。")
        return 0
    for k in keys:
        v = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()[0]
        print(f"\n{k} = {v if len(v) <= 800 else v[:800] + ' …(截断)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
