#!/usr/bin/env python3
"""10M 封库流水线分步计时：索引 → dir_rollup → optimize → FTS5。

用法：python scripts/bench10m_seal.py <data_root>
前置：bench10m_make.py 已生成两代快照库（仅表+数据，无索引）。
每步后记录 db 文件大小（含 FTS 增量）；末尾输出 JSON 到 <data_root>/seal_timings.json。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cold_manifest.schema import SNAPSHOT_INDEXES_DDL  # noqa: E402
from cold_manifest.seal import build_dir_rollup, build_fts  # noqa: E402

DATA_ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/bench10m")
NO_FTS = "--no-fts" in sys.argv
VOLUME_ID = "BENCH10M_P0"
TS = {"B1": "20260928T000001Z", "B2": "20260928T000002Z"}


def sized(db: Path) -> int:
    return db.stat().st_size


def run(tag: str) -> dict:
    db = DATA_ROOT / VOLUME_ID / TS[tag] / "snapshot.db"
    conn = sqlite3.connect(db.as_posix())
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    steps: "list[dict]" = []
    size0 = sized(db)

    def step(name: str, fn) -> None:
        t = time.monotonic()
        fn()
        el = time.monotonic() - t
        steps.append({"step": name, "seconds": round(el, 2),
                      "db_bytes": sized(db), "delta_bytes": sized(db) - size0})
        print(f"[{tag}] {name}: {el:.2f}s  db={sized(db)/2**30:.2f} GiB")

    step("indexes", lambda: conn.executescript(SNAPSHOT_INDEXES_DDL))
    step("dir_rollup", lambda: build_dir_rollup(conn))
    step("optimize", lambda: conn.execute("PRAGMA optimize"))
    if not NO_FTS:
        step("fts5", lambda: build_fts(conn))
    conn.commit()
    conn.close()
    return {"tag": tag, "db": str(db), "db_bytes_final": sized(db), "steps": steps}


if __name__ == "__main__":
    out = [run("B1"), run("B2")]
    (DATA_ROOT / "seal_timings.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print("已写", DATA_ROOT / "seal_timings.json")
