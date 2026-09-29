#!/usr/bin/env python3
"""10M 条目压测数据集生成（直接 SQL 造 snapshot.db，不造真实文件）。

产出：<data_root>/<volume_id>/<ts>/snapshot.db（B1/B2 两代，表+meta，未建索引未封库）
     <data_root>/catalog.db（disk/volume/snapshot 注册，status 由封库脚本封完后写 done）

B2 相对 B1 的改动（合计 ~1.9%，各集合互斥、RNG 可复现 seed=42）：
- size 改 5 万（hash 同步换 → content_changed）
- mtime 改 5 万（hash 不变 → mtime_changed）
- 删除 5 万（removed）
- 新增 5 万（added，哈希随机不与 removed 匹配）
- 改名 2 万（path 加 .ren 后缀，hash/size 不变 → 与 removed 配对 → moved_or_renamed）
- 同尺寸同 mtime 改写 2 万（只换 hash → content_changed）
- 重复内容压力：删除集中 1000 条与新增集中 1000 条共享同一 (hash, size)，
  _pair_moved 的 (hash,size) join 会产生 1000×1000=100 万候选对（热点观测点）

用法：python scripts/bench10m_make.py <data_root>
"""
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cold_manifest.schema import SNAPSHOT_TABLES_DDL  # noqa: E402

DATA_ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/bench10m")
VOLUME_ID = "BENCH10M_P0"
TS_A, TS_B = "20260928T000001Z", "20260928T000002Z"
SNAP_A, SNAP_B = f"{VOLUME_ID}/{TS_A}", f"{VOLUME_ID}/{TS_B}"

TOTAL_ENTRIES = 10_000_000
N_DIRS = 300_000          # 不含 root；depth ≤ 12
SEED = 42

EXTS = ["", "", "", "", ".jpg", ".bin", ".txt", ".pdf", ".mp4", ".zip", ".dat", ".log"]
DUP_HASH = "d" * 64
DUP_SIZE = 1_111_111


def build_b1(db_path: Path) -> None:
    """生成 B1：root + 30 万目录 + 969.9 万文件，批量 executemany。"""
    import random
    rng = random.Random(SEED)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path.as_posix())
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(SNAPSHOT_TABLES_DDL)

    t0 = time.monotonic()
    dirs = [(".", 0)]             # (path, depth)，下标 i → entry_id=i+1
    dirs_rows = [(1, 0, ".", ".", 0, "dir", None, None, None, None, None, None, None,
                  None, None, ".", None)]
    # 目录树：父目录从 depth<11 的既有目录里均匀挑
    while len(dirs) < N_DIRS + 1:
        pid = rng.randrange(len(dirs))
        while dirs[pid][1] >= 11:   # 目录最深层 11 → 文件最深层 12（depth ≤ 12）
            pid = rng.randrange(len(dirs))
        ppath, pdepth = dirs[pid]
        pentry = pid + 1            # 下标 i ↔ entry_id=i+1
        eid = len(dirs) + 1
        name = f"d{eid}"
        path = name if ppath == "." else f"{ppath}/{name}"
        dirs.append((path, pdepth + 1))
        dirs_rows.append((eid, pentry, path, name, pdepth + 1, "dir",
                          None, None, None, None, None, None, None, None, None,
                          path.casefold(), None))

    def file_rows():
        eid = N_DIRS + 2
        for n in range(TOTAL_ENTRIES - N_DIRS - 1):
            idx = rng.randrange(len(dirs))
            ppath, pdepth = dirs[idx]
            pentry = idx + 1
            ext = EXTS[rng.randrange(len(EXTS))]
            name = f"f{n}{ext}"
            path = f"{ppath}/{name}"
            size = 0 if rng.random() < 0.08 else int(2 ** rng.uniform(0, 30))
            alloc = ((size + 4095) // 4096) * 4096
            mtime = rng.randrange(1_450_000_000, 1_790_000_000) * 1_000_000_000
            h = hashlib.sha256(f"b1:{n}".encode()).hexdigest()
            yield (eid, pentry, path, name, pdepth + 1, "file",
                   size, alloc, mtime, mtime, mtime, 0o644,
                   ext.lower() if ext else "", h, "full", path.casefold(), None)
            eid += 1

    conn.executemany("INSERT INTO entries(entry_id,parent_id,path,name,depth,type,"
                     "size_bytes,allocated_bytes,mtime_ns,ctime_ns,btime_ns,attrs,"
                     "ext,hash_hex,hash_state,path_norm,error) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", dirs_rows)
    conn.commit()
    BATCH = 200_000
    batch = []
    for row in file_rows():
        batch.append(row)
        if len(batch) >= BATCH:
            conn.executemany("INSERT INTO entries(entry_id,parent_id,path,name,depth,type,"
                             "size_bytes,allocated_bytes,mtime_ns,ctime_ns,btime_ns,attrs,"
                             "ext,hash_hex,hash_state,path_norm,error) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            conn.commit()
            batch.clear()
    if batch:
        conn.executemany("INSERT INTO entries(entry_id,parent_id,path,name,depth,type,"
                         "size_bytes,allocated_bytes,mtime_ns,ctime_ns,btime_ns,attrs,"
                         "ext,hash_hex,hash_state,path_norm,error) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        conn.commit()
    print(f"B1 数据灌入 {time.monotonic()-t0:.1f}s")

    now = "2026-09-28T00:00:01Z"
    meta = [
        ("status", "sealed"),          # seal 脚本按分步计时重建索引/rollup/FTS
        ("root_path", str(db_path.parent)),
        ("scan_root", str(db_path.parent)),
        ("volume_id", VOLUME_ID),
        ("collected_at", now),
        ("collector_version", "bench10m"),
        ("resume_state", "complete"),
    ]
    conn.executemany("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", meta)
    conn.commit()
    conn.close()
    print(f"B1 完成（总耗时 {time.monotonic()-t0:.1f}s）→ {db_path}")


def build_b2(b1: Path, b2: Path) -> dict:
    """复制 B1 → B2，SQL 施加改动。返回期望 diff 计数（校验用）。"""
    import shutil
    import random
    rng = random.Random(SEED + 1)
    t0 = time.monotonic()
    b2.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(b1, b2)
    conn = sqlite3.connect(b2.as_posix())
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")

    first_file, last_file = N_DIRS + 2, TOTAL_ENTRIES  # 文件 entry_id 连续区间

    pool = rng.sample(range(first_file, last_file + 1), 190_000)  # 五个集合互斥
    ids_size, ids_mtime, ids_del = pool[:50_000], pool[50_000:100_000], pool[100_000:150_000]
    ids_ren, ids_rw = pool[150_000:170_000], pool[170_000:190_000]
    ids_del_dup = rng.sample(ids_del, 1000)

    # 1) size 改（hash 同步换 → content_changed）
    conn.executemany(
        "UPDATE entries SET size_bytes=size_bytes+12345,"
        " allocated_bytes=((size_bytes+12345+4095)/4096)*4096,"
        " hash_hex=? WHERE entry_id=?",
        [(hashlib.sha256(f"sz:{i}".encode()).hexdigest(), i) for i in ids_size])
    # 2) mtime 改（hash 不变 → mtime_changed）
    conn.executemany("UPDATE entries SET mtime_ns=mtime_ns+3600000000000 WHERE entry_id=?",
                     [(i,) for i in ids_mtime])
    # 3) 同尺寸同 mtime 改写（只换 hash → content_changed）
    conn.executemany("UPDATE entries SET hash_hex=? WHERE entry_id=?",
                     [(hashlib.sha256(f"rw:{i}".encode()).hexdigest(), i) for i in ids_rw])
    # 4) 改名：path/name/path_norm 加 .ren 后缀（hash/size 不变 → moved 配对）
    conn.executemany(
        "UPDATE entries SET path=path||'.ren', name=name||'.ren',"
        " path_norm=path_norm||'.ren' WHERE entry_id=?", [(i,) for i in ids_ren])
    # 5) 重复内容压力：删除集中 1000 条统一 (hash,size)
    conn.executemany("UPDATE entries SET hash_hex=?, size_bytes=?, allocated_bytes=?"
                     " WHERE entry_id=?",
                     [(DUP_HASH, DUP_SIZE, DUP_SIZE, i) for i in ids_del_dup])

    # 6) 删除 5 万
    conn.executemany("DELETE FROM entries WHERE entry_id=?", [(i,) for i in ids_del])

    # 7) 新增 5 万：随机挂在现存目录下，fa{n}.new
    dir_rows = conn.execute("SELECT entry_id, path, depth FROM entries WHERE type='dir'").fetchall()
    max_id = conn.execute("SELECT MAX(entry_id) FROM entries").fetchone()[0]
    eid = max_id + 1
    added = []
    for n in range(50_000):
        de, dpath, ddepth = dir_rows[rng.randrange(len(dir_rows))]
        path = f"{dpath}/fa{n}.new"
        h = hashlib.sha256(f"add:{n}".encode()).hexdigest()
        if n < 1000:                       # 与删除集中 1000 条共享 (hash,size)
            h, size = DUP_HASH, DUP_SIZE
        else:
            size = int(2 ** rng.uniform(0, 30))
        added.append((eid, de, path, f"fa{n}.new", ddepth + 1, "file",
                      size, ((size + 4095) // 4096) * 4096,
                      1_780_000_000 * 1_000_000_000, None, None, 0o644, ".new",
                      h, "full", path.casefold(), None))
        eid += 1
    conn.executemany("INSERT INTO entries(entry_id,parent_id,path,name,depth,type,"
                     "size_bytes,allocated_bytes,mtime_ns,ctime_ns,btime_ns,attrs,"
                     "ext,hash_hex,hash_state,path_norm,error) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", added)
    # 8) 重复内容压力的另一半：A 侧同样 1000 条统一 (hash,size)。
    #    removed 行的哈希取自 A，两侧都改才能让 _pair_moved 的 (hash,size)
    #    join 命中，产生 1000×1000=100 万候选对（热点观测点）。
    a_conn = sqlite3.connect(b1.as_posix())
    a_conn.execute("PRAGMA journal_mode=OFF")
    a_conn.executemany("UPDATE entries SET hash_hex=?, size_bytes=?, allocated_bytes=?"
                       " WHERE entry_id=?",
                       [(DUP_HASH, DUP_SIZE, DUP_SIZE, i) for i in ids_del_dup])
    a_conn.commit()
    a_conn.close()
    conn.commit()
    conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('collected_at',?)",
                 ("2026-09-28T00:00:02Z",))
    conn.commit()
    conn.close()
    print(f"B2 改动施加 {time.monotonic()-t0:.1f}s → {b2}")
    return {"expected": {
        "content_changed": 70_000, "mtime_changed": 50_000,
        "moved_or_renamed": 21_000, "removed": 49_000, "added": 49_000,
        "identical": 9_810_000,
    }}


def register(data_root: Path) -> None:
    """注册 catalog（disk/volume/snapshots，统计从库内聚合）。"""
    from cold_manifest.db import init_catalog
    cat = sqlite3.connect((data_root / "catalog.db").as_posix())
    init_catalog(cat)
    now = "2026-09-28T00:00:01Z"
    cat.execute("INSERT OR REPLACE INTO disks(disk_id,physical_model,first_seen,last_seen)"
                " VALUES(?,?,?,?)", ("BENCH10M_DISK", "BENCH SSD", now, now))
    cat.execute("INSERT OR REPLACE INTO volumes(volume_id,disk_id,partition_index,"
                "filesystem,capacity_bytes) VALUES(?,?,?,?,?)",
                (VOLUME_ID, "BENCH10M_DISK", 1, "ext4", 2 * 1024**4))
    for snap, ts in ((SNAP_A, TS_A), (SNAP_B, TS_B)):
        db = data_root / VOLUME_ID / ts / "snapshot.db"
        c = sqlite3.connect(db.as_posix())
        agg = c.execute("SELECT COUNT(*) FILTER (WHERE type='file'),"
                        " COUNT(*) FILTER (WHERE type='dir'),"
                        " COALESCE(SUM(size_bytes) FILTER (WHERE type='file'),0),"
                        " MAX(depth) FROM entries").fetchone()
        c.close()
        fc, dc, tb, md = (int(agg[0]), int(agg[1]), int(agg[2]), int(agg[3] or 0))
        cat.execute(
            "INSERT OR REPLACE INTO snapshots(snapshot_id,volume_id,collected_at,"
            "collector_version,host_path,status,file_count,dir_count,total_bytes,"
            "total_alloc,zero_byte_count,max_depth) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (snap, VOLUME_ID, now, "bench10m", str(data_root / VOLUME_ID / ts),
             "sealed", fc, dc, tb, tb, 0, md))
    cat.commit()
    cat.close()
    print("catalog 注册完成")


if __name__ == "__main__":
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    b1 = DATA_ROOT / VOLUME_ID / TS_A / "snapshot.db"
    b2 = DATA_ROOT / VOLUME_ID / TS_B / "snapshot.db"
    if not b1.exists():
        build_b1(b1)
    else:
        print(f"跳过 B1（已存在）：{b1}")
    if not b2.exists():
        exp = build_b2(b1, b2)
        (DATA_ROOT / "expected_counts.json").write_text(json.dumps(exp, indent=1))
    else:
        print(f"跳过 B2（已存在）：{b2}")
    register(DATA_ROOT)
