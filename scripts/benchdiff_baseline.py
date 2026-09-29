#!/usr/bin/env python3
"""diff 物化基准夹具 + 基准 dump（SQL 下推改造的逐字节比对用）。

用法：
    python scripts/benchdiff_baseline.py dump <out.dump>   # 造夹具并物化，dump diff_entries
    python scripts/benchdiff_baseline.py dumpnone <out.dump>  # 同上但 hash=none
    python scripts/benchdiff_baseline.py fixture <dir>     # 只造夹具

夹具覆盖：identical / size_changed / mtime_changed / type_changed /
content_changed（full 哈希、同尺寸同 mtime）/ hash 同则忽略 size 判 identical /
sampled 回退 size-mtime 语义 / moved_or_renamed（含重复哈希确定性一对一）/
removed+sampled（哈希不可用不落 a_hash）/ 目录行 NULL size/mtime 的 NULL 等值 /
added 目录（派生目录）。所有条目 path_norm 非空（走 SQL 快路径）；
NULL path_norm 的回退路径由 tests/test_diff.py 与 dumpnone 变体覆盖。
"""
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from cold_manifest.schema import SNAPSHOT_TABLES_DDL  # noqa: E402
from cold_manifest.diff_engine import materialize_diff  # noqa: E402

IDX_DDL = """
CREATE UNIQUE INDEX uq_entries_path ON entries(path);
CREATE INDEX idx_entries_pathnorm ON entries(path_norm);
"""

EID = [100]


def _mk_snap(path: Path, rows):
    EID[0] = 100
    import sqlite3
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    conn.executescript(SNAPSHOT_TABLES_DDL)
    conn.executescript(IDX_DDL)
    conn.execute("INSERT INTO meta(key, value) VALUES('status','sealed')")
    for r in rows:
        EID[0] += 1
        conn.execute(
            "INSERT INTO entries(entry_id,parent_id,path,name,depth,type,size_bytes,"
            "mtime_ns,hash_algo,hash_hex,hash_state,path_norm)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (EID[0], 1, r["path"], r["path"].rsplit("/", 1)[-1],
             r["path"].count("/") + 1, r["type"], r.get("size"),
             r.get("mtime"), "sha256" if r.get("hash") else None,
             r.get("hash"), r.get("hash_state"),
             (r["path"] + "x").casefold() if r.get("pn_null") is None else r.get("pn_null")))
    conn.commit()
    conn.close()


H1 = hashlib.sha256(b"alpha").hexdigest()
H2 = hashlib.sha256(b"bravo").hexdigest()
HDUP = hashlib.sha256(b"dup").hexdigest()

A_ROWS = [
    # 两侧同：identical（文件，含 mtime）
    {"path": "a/identical.txt", "type": "file", "size": 10, "mtime": 111, "hash": H1, "hash_state": "full"},
    # size_changed
    {"path": "a/size.txt", "type": "file", "size": 10, "mtime": 111},
    # mtime_changed（同尺寸）
    {"path": "a/mtime.txt", "type": "file", "size": 10, "mtime": 111},
    # type_changed：file -> dir
    {"path": "a/type.txt", "type": "file", "size": 5, "mtime": 111},
    # content_changed：full 哈希、同尺寸同 mtime、哈希不同
    {"path": "a/content.txt", "type": "file", "size": 7, "mtime": 222, "hash": H1, "hash_state": "full"},
    # hash 同 + mtime 同，但 size 不同 -> hash 分支忽略 size -> identical
    {"path": "a/hashignore.txt", "type": "file", "size": 99, "mtime": 333, "hash": H2, "hash_state": "full"},
    # sampled 两侧：回退 size/mtime（同尺寸同 mtime -> identical，不得 content_changed）
    {"path": "a/sampled_same.txt", "type": "file", "size": 8, "mtime": 444, "hash": H1, "hash_state": "sampled"},
    # sampled 两侧、mtime 不同 -> mtime_changed
    {"path": "a/sampled_mtime.txt", "type": "file", "size": 8, "mtime": 444, "hash": H1, "hash_state": "sampled"},
    # removed：full 哈希，与 B 侧 added 配成 moved
    {"path": "old/moved.txt", "type": "file", "size": 12, "mtime": 555, "hash": HDUP, "hash_state": "full"},
    # removed：重复哈希候选之一（与 old2/dup*.txt 配对竞争）
    {"path": "old/dup_a.txt", "type": "file", "size": 6, "mtime": 666, "hash": H2, "hash_state": "full"},
    {"path": "old/dup_b.txt", "type": "file", "size": 6, "mtime": 667, "hash": H2, "hash_state": "full"},
    # removed：sampled 哈希不可用 -> a_hash 不落，保持 removed
    {"path": "old/stays_removed.txt", "type": "file", "size": 3, "mtime": 777, "hash": H1, "hash_state": "sampled"},
    # removed 目录（派生目录）
    {"path": "old_dir", "type": "dir", "size": None, "mtime": 888},
    # 两侧同：目录 NULL size/mtime 的 NULL 等值 -> identical
    {"path": "dir_both", "type": "dir", "size": None, "mtime": None},
    # 两侧同：error 哈希 -> 回退 size/mtime
    {"path": "a/errstate.txt", "type": "file", "size": 4, "mtime": 999, "hash": H1, "hash_state": "error"},
]

B_ROWS = [
    {"path": "a/identical.txt", "type": "file", "size": 10, "mtime": 111, "hash": H1, "hash_state": "full"},
    {"path": "a/size.txt", "type": "file", "size": 20, "mtime": 111},
    {"path": "a/mtime.txt", "type": "file", "size": 10, "mtime": 112},
    {"path": "a/type.txt", "type": "dir", "size": None, "mtime": 111},
    {"path": "a/content.txt", "type": "file", "size": 7, "mtime": 222, "hash": H2, "hash_state": "full"},
    {"path": "a/hashignore.txt", "type": "file", "size": 55, "mtime": 333, "hash": H2, "hash_state": "full"},
    {"path": "a/sampled_same.txt", "type": "file", "size": 8, "mtime": 444, "hash": H2, "hash_state": "sampled"},
    {"path": "a/sampled_mtime.txt", "type": "file", "size": 8, "mtime": 445, "hash": H2, "hash_state": "sampled"},
    # added：与 old/moved.txt 配 moved_or_renamed
    {"path": "new/moved.txt", "type": "file", "size": 12, "mtime": 556, "hash": HDUP, "hash_state": "full"},
    # 重复候选：B 侧两个同 (hash,size)，确定性一对一后剩一个 added
    {"path": "new/dup_b.txt", "type": "file", "size": 6, "mtime": 666, "hash": H2, "hash_state": "full"},
    {"path": "new/dup_a.txt", "type": "file", "size": 6, "mtime": 667, "hash": H2, "hash_state": "full"},
    # added：普通新增目录（派生）
    {"path": "new_dir", "type": "dir", "size": None, "mtime": 123},
    # added：带 full 哈希的普通新增（b_hash 必须落库，且不与任何 removed 配对）
    {"path": "new/plain_added.txt", "type": "file", "size": 21, "mtime": 321,
     "hash": hashlib.sha256(b"charlie").hexdigest(), "hash_state": "full"},
    # added：sampled 哈希不可用 -> b_hash 不落
    {"path": "new/sampled_added.txt", "type": "file", "size": 22, "mtime": 322,
     "hash": H1, "hash_state": "sampled"},
    {"path": "dir_both", "type": "dir", "size": None, "mtime": None},
    {"path": "a/errstate.txt", "type": "file", "size": 4, "mtime": 999, "hash": H2, "hash_state": "error"},
]


def build_fixture(root: Path) -> None:
    import shutil
    root.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(root / "_diffs", ignore_errors=True)   # 防止同 diff_id 幂等复用旧结果
    vol = root / "VOLA" / "20260101T000000Z"
    vol.mkdir(parents=True, exist_ok=True)
    _mk_snap(vol / "snapshot.db", A_ROWS)
    vol = root / "VOLB" / "20260101T000000Z"
    vol.mkdir(parents=True, exist_ok=True)
    _mk_snap(vol / "snapshot.db", B_ROWS)


def dump_diff_db(db: Path, out: Path) -> None:
    import sqlite3
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT change_type, path, path_norm, depth, a_entry_id, b_entry_id,"
            " a_size, b_size, a_mtime_ns, b_mtime_ns, a_type, b_type,"
            " a_hash, b_hash, b_path FROM diff_entries"
            " ORDER BY path, change_type, b_path").fetchall()
        summary = conn.execute(
            "SELECT value FROM diff_meta WHERE key='summary_json'").fetchone()
    finally:
        conn.close()
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
        f.write("SUMMARY " + (summary[0] if summary else "NONE") + "\n")


def main() -> None:
    mode, arg = sys.argv[1], sys.argv[2]
    root = Path("/tmp/opencode/benchdiff_fixture")
    if mode == "fixture":
        build_fixture(root)
        print(f"fixture at {root}")
        return
    hash_opt = "none" if mode == "dumpnone" else "sha256"
    build_fixture(root)
    res = materialize_diff(root, "VOLA/20260101T000000Z", "VOLB/20260101T000000Z", {"hash": hash_opt})
    print("counts:", json.dumps(res.counts, sort_keys=True))
    dump_diff_db(res.db_path, Path(arg))
    print(f"dump -> {arg}")


if __name__ == "__main__":
    main()
