"""Diff 引擎（§4.5/§5.3）：物化两快照差异到 data/_diffs/<diff_id>.db。

语义：
- 仅 A 有 → removed；仅 B 有 → added；
- 两侧都有依次判 type_changed → size_changed → mtime_changed → identical；
- identical 不落库只计数；无哈希时 content_changed / moved_or_renamed 恒为 NULL（不造假）。
- 按 path 精确 join：两个快照库各按 uq_entries_path 索引流式归并，内存 O(1)。

写入风格与导入一致：journal_mode=OFF、批量事务、封库时建 DIFF_DDL 索引。
"""

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .catalog import connect_catalog, snapshot_path
from .db import open_snapshot
from .schema import DIFF_DDL

_BATCH_SIZE = 10_000

# 需要落库/计数的分类（identical 只计数；content/moved 无哈希恒 NULL）
_COUNTED = ("added", "removed", "type_changed", "size_changed", "mtime_changed", "identical")


class DiffError(Exception):
    """快照缺失、未封库等 diff 前置条件失败。"""


@dataclass
class DiffResult:
    diff_id: str
    a: str
    b: str
    db_path: Path
    options_json: str
    options_hash: str
    counts: dict = field(default_factory=dict)   # 含 content_changed/moved_or_renamed=None
    reused: bool = False
    elapsed_s: float = 0.0


def canonical_options(options: "dict | None") -> dict:
    """选项白名单：当前版本仅支持 {"hash": "none"}（哈希采集未启用）。

    未知键或非法值抛 DiffError（路由层转 400）——静默接受会让非 canonical
    options 污染幂等键，产生永不复用的重复 diff 库。
    """
    opts = dict(options or {})
    unknown = set(opts) - {"hash"}
    if unknown:
        raise DiffError(f"不支持的 diff 选项：{sorted(unknown)}（当前仅支持 hash）")
    if opts.get("hash", "none") != "none":
        raise DiffError(f"不支持的 hash 选项值：{opts['hash']!r}（当前仅支持 'none'）")
    opts["hash"] = "none"
    return opts


def compute_options_hash(options: "dict | None") -> str:
    canonical = json.dumps(canonical_options(options), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_diff_id(a: str, b: str, options_hash: str) -> str:
    return hashlib.sha1(f"{a}|{b}|{options_hash}".encode("utf-8")).hexdigest()[:16]


def diff_db_path(data_root: "str | Path", diff_id: str) -> Path:
    return Path(data_root) / "_diffs" / f"{diff_id}.db"


def _sealed_snapshot(data_root: "str | Path", snapshot_id: str) -> Path:
    """校验快照存在且封库，返回库路径。"""
    try:
        db = snapshot_path(data_root, snapshot_id)
    except Exception as e:
        raise DiffError(f"非法快照 ID：{snapshot_id!r}（{e}）") from e
    if not db.is_file():
        raise DiffError(f"快照不存在：{snapshot_id}（{db}）")
    conn = open_snapshot(db)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='status'").fetchone()
        if not row or row[0] != "sealed":
            raise DiffError(f"快照未封库，不能参与 diff：{snapshot_id}")
    finally:
        conn.close()
    return db


def _iter_entries(db: Path):
    """按 path 有序流式产出 (path, entry_id, path_norm, depth, type, size, mtime_ns)。

    依赖封库时建的 uq_entries_path 索引做索引扫描，不排序、不整表载入。
    """
    conn = open_snapshot(db)
    try:
        cur = conn.execute(
            "SELECT path, entry_id, path_norm, depth, type, size_bytes, mtime_ns "
            "FROM entries ORDER BY path"
        )
        while True:
            rows = cur.fetchmany(8192)
            if not rows:
                return
            yield from rows
    finally:
        conn.close()


def _classify(ra, rb) -> str:
    if ra[4] != rb[4]:
        return "type_changed"
    if ra[5] != rb[5]:
        return "size_changed"
    if ra[6] != rb[6]:
        return "mtime_changed"
    return "identical"


def _read_existing_status(db: Path) -> "str | None":
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM diff_meta WHERE key='status'").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def materialize_diff(data_root: "str | Path", a: str, b: str,
                     options: "dict | None" = None, force: bool = False) -> DiffResult:
    """物化 diff(a, b, options)，幂等复用已封库结果。见模块 docstring。"""
    t0 = time.monotonic()
    data_root = Path(data_root)
    opts = canonical_options(options)
    ohash = compute_options_hash(opts)
    ojson = json.dumps(opts, sort_keys=True, separators=(",", ":"))
    did = compute_diff_id(a, b, ohash)
    out_db = diff_db_path(data_root, did)

    if not force and _read_existing_status(out_db) == "done":
        counts = _read_summary(out_db)
        return DiffResult(diff_id=did, a=a, b=b, db_path=out_db, options_json=ojson,
                          options_hash=ohash, counts=counts, reused=True,
                          elapsed_s=time.monotonic() - t0)

    db_a = _sealed_snapshot(data_root, a)
    db_b = _sealed_snapshot(data_root, b)

    out_db.parent.mkdir(parents=True, exist_ok=True)
    # 未封库残留（上次运行崩溃）一律删除重建
    for stale in (out_db, Path(str(out_db) + "-wal"), Path(str(out_db) + "-shm")):
        stale.unlink(missing_ok=True)

    counts = {k: 0 for k in _COUNTED}
    counts["content_changed"] = None    # 无哈希：不造假
    counts["moved_or_renamed"] = None

    conn = sqlite3.connect(str(out_db))
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS diff_meta(key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE IF NOT EXISTS diff_entries("
            " id INTEGER PRIMARY KEY, change_type TEXT NOT NULL, path TEXT NOT NULL,"
            " path_norm TEXT NOT NULL, depth INTEGER NOT NULL,"
            " a_entry_id INTEGER, b_entry_id INTEGER,"
            " a_size INTEGER, b_size INTEGER, a_mtime_ns INTEGER, b_mtime_ns INTEGER,"
            " a_type TEXT, b_type TEXT);"
        )

        it_a = _iter_entries(db_a)
        it_b = _iter_entries(db_b)
        ra = next(it_a, None)
        rb = next(it_b, None)
        buf: "list[tuple]" = []

        def flush() -> None:
            if buf:
                conn.executemany(
                    "INSERT INTO diff_entries(change_type,path,path_norm,depth,a_entry_id,b_entry_id,"
                    "a_size,b_size,a_mtime_ns,b_mtime_ns,a_type,b_type)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", buf)
                buf.clear()

        while ra is not None or rb is not None:
            if rb is None or (ra is not None and ra[0] < rb[0]):      # 仅 A 有
                counts["removed"] += 1
                buf.append(("removed", ra[0], ra[2] or ra[0].casefold(), ra[3],
                            ra[1], None, ra[5], None, ra[6], None, ra[4], None))
                ra = next(it_a, None)
            elif ra is None or rb[0] < ra[0]:                          # 仅 B 有
                counts["added"] += 1
                buf.append(("added", rb[0], rb[2] or rb[0].casefold(), rb[3],
                            None, rb[1], None, rb[5], None, rb[6], None, rb[4]))
                rb = next(it_b, None)
            else:                                                      # 两侧都有
                ct = _classify(ra, rb)
                counts[ct] += 1
                if ct != "identical":
                    buf.append((ct, ra[0], ra[2] or ra[0].casefold(), ra[3],
                                ra[1], rb[1], ra[5], rb[5], ra[6], rb[6], ra[4], rb[4]))
                ra = next(it_a, None)
                rb = next(it_b, None)
            if len(buf) >= _BATCH_SIZE:
                flush()
                conn.commit()
        flush()
        conn.commit()

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta_rows = [
            ("a", a), ("b", b), ("options_json", ojson), ("options_hash", ohash),
            ("created_at", now), ("status", "done"),
            ("summary_json", json.dumps(counts, sort_keys=True)),
        ]
        conn.executemany("INSERT OR REPLACE INTO diff_meta(key, value) VALUES(?, ?)", meta_rows)
        conn.commit()
        # 封库：建 DIFF_DDL 索引（含 IF NOT EXISTS，幂等）
        conn.executescript(DIFF_DDL)
        conn.execute("PRAGMA optimize")
        conn.commit()
    finally:
        conn.close()

    elapsed = time.monotonic() - t0

    # catalog 登记（本模块内直接 SQL，不改 catalog.py）
    cat = connect_catalog(data_root)
    try:
        cat.execute(
            "INSERT OR REPLACE INTO diff_runs(diff_id, a, b, options_hash, options_json,"
            " created_at, duration_ms, status, summary_json, result_path) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (did, a, b, ohash, ojson, now, int(elapsed * 1000), "done",
             json.dumps(counts, sort_keys=True), str(out_db)))
        cat.commit()
    finally:
        cat.close()

    return DiffResult(diff_id=did, a=a, b=b, db_path=out_db, options_json=ojson,
                      options_hash=ohash, counts=counts, reused=False, elapsed_s=elapsed)


def _read_summary(db: Path) -> dict:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM diff_meta WHERE key='summary_json'").fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        conn.close()


def iter_diff_csv(db: Path, depth: "int | None" = None):
    """流式产出 CSV 行 (category, path, type, size_a, size_b, mtime_a, mtime_b)。

    type 取 A 侧（缺失取 B 侧）；type_changed 两侧类型都存在且不同时记 'a->b'
    （类型直接读 diff_entries.a_type/b_type，物化时已填充）。
    """
    main = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        where = "WHERE depth <= ?" if depth is not None else ""
        params = (depth,) if depth is not None else ()
        cur = main.execute(
            f"""
            SELECT change_type AS category, path AS path,
                   CASE WHEN a_type IS NOT NULL AND b_type IS NOT NULL AND a_type != b_type
                        THEN a_type || '->' || b_type
                        ELSE COALESCE(a_type, b_type) END AS type,
                   a_size AS size_a, b_size AS size_b,
                   a_mtime_ns AS mtime_a, b_mtime_ns AS mtime_b
            FROM diff_entries
            {where}
            ORDER BY change_type, path
            """,
            params,
        )
        while True:
            rows = cur.fetchmany(8192)
            if not rows:
                return
            yield from rows
    finally:
        main.close()
