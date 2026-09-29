"""Diff 引擎（§4.5/§5.3）：物化两快照差异到 data/_diffs/<diff_id>.db。

语义：
- 仅 A 有 → removed；仅 B 有 → added；
- 两侧都有：类型不同 → type_changed；
  - hash 启用且两侧哈希均可用（hash_hex 非空且 hash_state == 'full'，sampled
    只读首尾 64KB 不算内容等值证明）时：
    hash 不同 → content_changed（优先于 size/mtime，专抓"同尺寸同 mtime 改写"）；
    hash 相同 → 再按 mtime 判 mtime_changed / identical（此时忽略 size）；
  - hash 未启用或任一侧哈希不可用 → 依次 size_changed → mtime_changed → identical；
- moved_or_renamed（仅 hash 启用）：对 removed / added 中哈希可用的文件条目按
  (hash_hex, size_bytes) 配对，A/B 路径不同 → 合并为一行 moved_or_renamed
  （path 取 A 侧，b_path 记 B 侧新路径）；配不上的仍归 removed / added。
  配对走 diff_entries 上的 SQL join + 临时表，不整表载入内存。
- identical 不落库只计数；hash=none 时 content_changed / moved_or_renamed
  恒为 NULL（不造假）。
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
    """选项白名单：{"hash": "none" | "sha256"}（§4.5 哈希联动 diff）。

    未知键或非法值抛 DiffError（路由层转 400）——静默接受会让非 canonical
    options 污染幂等键，产生永不复用的重复 diff 库。hash 进入 options_hash，
    因此 hash=none 与 hash=sha256 的 diff_id 天然隔离、各自幂等复用。
    """
    opts = dict(options or {})
    unknown = set(opts) - {"hash"}
    if unknown:
        raise DiffError(f"不支持的 diff 选项：{sorted(unknown)}（当前仅支持 hash）")
    algo = opts.get("hash", "none")
    if algo not in ("none", "sha256"):
        raise DiffError(f"不支持的 hash 选项值：{algo!r}（允许 'none' / 'sha256'）")
    opts["hash"] = algo
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
    """按 path 有序流式产出 (path, entry_id, path_norm, depth, type, size, mtime,
    hash_hex, hash_state)。

    依赖封库时建的 uq_entries_path 索引做索引扫描，不排序、不整表载入。
    """
    conn = open_snapshot(db)
    try:
        cur = conn.execute(
            "SELECT path, entry_id, path_norm, depth, type, size_bytes, mtime_ns,"
            " hash_hex, hash_state FROM entries ORDER BY path"
        )
        while True:
            rows = cur.fetchmany(8192)
            if not rows:
                return
            yield from rows
    finally:
        conn.close()


def _hash_usable(row) -> bool:
    """该条目哈希可用于内容等值比对：有 hash_hex 且 hash_state == 'full'。

    sampled 只读首尾 64KB，不是内容等值证明——不能据此判 content_changed /
    moved_or_renamed；'cached'（旧库遗留）同样不可信，一律回退 size/mtime 语义。
    """
    return bool(row[7]) and row[8] == "full"


def _classify(ra, rb, hash_enabled: bool) -> str:
    if ra[4] != rb[4]:
        return "type_changed"
    if hash_enabled and _hash_usable(ra) and _hash_usable(rb):
        if ra[7] != rb[7]:
            return "content_changed"      # 同尺寸同 mtime 改写也逃不掉
        return "mtime_changed" if ra[6] != rb[6] else "identical"
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


def _pair_moved(conn: sqlite3.Connection, counts: dict) -> None:
    """hash 启用后的 moved_or_renamed 配对（§4.5）。

    对 removed / added 中哈希可用的条目按 (hash_hex, size_bytes) SQL join 出
    候选对（差异通常是少数，候选集走临时表不整表载入），再确定性地做一对一
    匹配（同名重复内容按 path 序取第一个未占用的），A/B 路径不同 → 合并为
    moved_or_renamed 一行：path 保留 A 侧，b_path 记 B 侧新路径，并回填 B 侧
    entry_id/size/mtime/type/hash；配不上的维持 removed / added。
    """
    conn.execute(
        "CREATE TEMP TABLE _mv_pairs(rid INTEGER, aid INTEGER,"
        " PRIMARY KEY(rid, aid)) WITHOUT ROWID")
    conn.execute(
        """
        INSERT INTO _mv_pairs(rid, aid)
        SELECT r.id, ad.id
        FROM diff_entries r
        JOIN diff_entries ad
          ON ad.change_type = 'added'
         AND ad.b_hash = r.a_hash
         AND ad.b_size IS r.a_size
        WHERE r.change_type = 'removed'
          AND r.a_hash IS NOT NULL
          AND r.path != ad.path
        ORDER BY r.path, ad.path
        """)
    cur = conn.execute(
        "SELECT mp.rid, mp.aid FROM _mv_pairs mp"
        " JOIN diff_entries r ON r.id = mp.rid"
        " JOIN diff_entries ad ON ad.id = mp.aid"
        " ORDER BY r.path, ad.path")
    used_r: set = set()
    used_a: set = set()
    merged: "list[tuple[int, int]]" = []
    while True:
        rows = cur.fetchmany(8192)
        if not rows:
            break
        for rid, aid in rows:
            if rid in used_r or aid in used_a:
                continue
            used_r.add(rid)
            used_a.add(aid)
            merged.append((rid, aid))
    conn.execute("DROP TABLE _mv_pairs")
    for rid, aid in merged:
        rb = conn.execute(
            "SELECT b_entry_id, b_size, b_mtime_ns, b_type, b_hash, path"
            " FROM diff_entries WHERE id=?", (aid,)).fetchone()
        conn.execute(
            "UPDATE diff_entries SET change_type='moved_or_renamed',"
            " b_entry_id=?, b_size=?, b_mtime_ns=?, b_type=?, b_hash=?, b_path=?"
            " WHERE id=?",
            (rb[0], rb[1], rb[2], rb[3], rb[4], rb[5], rid))
        conn.execute("DELETE FROM diff_entries WHERE id=?", (aid,))
        counts["removed"] -= 1
        counts["added"] -= 1
        counts["moved_or_renamed"] += 1
    if merged:
        conn.commit()


def _cls_expr(alias_a: str, alias_b: str, colmap: "dict | None" = None) -> str:
    """分类 CASE 表达式（与 _classify 逐分支等价）。

    :hashp 参数为 1/0（hash 是否启用）。NULL 比较统一用 IS / IS NOT——
    Python 的 None != None 为 False（视为相等），IS 语义一致。
    colmap：B 侧别名列名映射（bside 紧凑副本的列名与 entries 不同）。
    """
    A, B = alias_a, alias_b
    m = colmap or {}
    B_id, B_type = m.get("entry_id", f'{B}.entry_id'), m.get("type", f'{B}."type"')
    B_size, B_mtime = m.get("size", f"{B}.size_bytes"), m.get("mtime", f"{B}.mtime_ns")
    B_hash, B_state = m.get("hash", f"{B}.hash_hex"), m.get("state", f"{B}.hash_state")
    return f"""
CASE
  WHEN {B_id} IS NULL THEN 'removed'
  WHEN {A}."type" IS NOT {B_type} THEN 'type_changed'
  WHEN ?1 AND {A}.hash_hex IS NOT NULL AND {A}.hash_state = 'full'
       AND {B_hash} IS NOT NULL AND {B_state} = 'full'
    THEN CASE WHEN {A}.hash_hex IS NOT {B_hash} THEN 'content_changed'
              WHEN {A}.mtime_ns IS NOT {B_mtime} THEN 'mtime_changed'
              ELSE 'identical' END
  WHEN {A}.size_bytes IS NOT {B_size} THEN 'size_changed'
  WHEN {A}.mtime_ns IS NOT {B_mtime} THEN 'mtime_changed'
  ELSE 'identical'
END"""


_BSIDE_COLS = {"entry_id": "bs.id", "type": "bs.ptype", "size": "bs.size",
               "mtime": "bs.mtime", "hash": "bs.hash", "state": "bs.hstate"}


_INSERT_COLS = (
    "INSERT INTO diff_entries(change_type,path,path_norm,depth,a_entry_id,b_entry_id,"
    "a_size,b_size,a_mtime_ns,b_mtime_ns,a_type,b_type,a_hash,b_hash,b_path)"
)


def _materialize_sql(conn: sqlite3.Connection, db_a: Path, db_b: Path,
                     hash_enabled: bool) -> dict:
    """SQL 下推归并（全顺序 IO 版）。

    朴素「A LEFT JOIN B + 逐行取行数据」在 10M×2 上受随机行读支配
    （HDD/WSL-VHD 实测 ~43µs/行，整场 380-620s，与旧 Python 归并无异）。
    本实现把所有大 IO 都排成顺序流，随机访问只落在新写的热页/小索引上：

    1. pairs：path 等值 join 只走两侧 uq_entries_path 索引（index-only 顺序扫，
       不取行数据），产出 (a_entry_id, b_entry_id)；
       removed/added 用 index-only 反连接各产出一个 id 集合；
    2. bside：B 侧行数据紧凑副本（顺序扫 B 一遍），刚写完、页缓存热，
       后续按 rowid 探测不再打冷盘；
    3. matched 分类落库：CROSS JOIN 固定 join 顺序——顺序扫 A 表 →
       pairs(aid) 索引（热）→ bside rowid（热）；
    4. identical 只计数，且不再二次扫描：identical = pairs 总对数 −
       已落库 matched 各类之和。

    语义与 Python 归并逐项一致（见模块 docstring 与 _classify）：
    - path 精确等值（uq_entries_path 为 BINARY collation，与 Python 字符串
      等值/排序一致）；
    - removed/added 的哈希只在 hash 启用且 _hash_usable 时落库；matched 的
      a_hash/b_hash 只要 hash 启用就落原始值（含 sampled，与旧实现一致，
      分类由 CASE 的 usable 条件守门）；
    - diff_entries.id 的顺序与旧实现不同（按 A 表 rowid 序而非全局 path 序）；
      所有读取路径（CSV/报告/API）都按显式列排序，唯一受 id 序影响的是
      _pair_moved 的贪心遍历，已改为显式 ORDER BY path（语义不变，且不再
      依赖插入顺序）。
    要求两侧 entries.path_norm 全部非空非 ''（materialize_diff 预检，否则走
    Python 归并回退——SQL 无法复现 casefold）。
    """
    counts = {k: 0 for k in _COUNTED}
    counts["content_changed"] = 0 if hash_enabled else None
    counts["moved_or_renamed"] = 0 if hash_enabled else None
    h = 1 if hash_enabled else 0
    cls = _cls_expr("sa", "bs", _BSIDE_COLS)
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA cache_size=-65536")
    try:
        conn.execute("ATTACH DATABASE ? AS sna",
                     (f"file:{db_a.as_posix()}?mode=ro&immutable=1",))
        conn.execute("ATTACH DATABASE ? AS snb",
                     (f"file:{db_b.as_posix()}?mode=ro&immutable=1",))

        # 1. matched 对（index-only，顺序）+ removed/added id 集合（index-only 反连接）
        conn.executescript(
            "CREATE TEMP TABLE pairs(aid INTEGER, bid INTEGER);"
            "CREATE TEMP TABLE rem_ids(id INTEGER PRIMARY KEY) WITHOUT ROWID;"
            "CREATE TEMP TABLE add_ids(id INTEGER PRIMARY KEY) WITHOUT ROWID;")
        conn.execute("INSERT INTO pairs(aid, bid)"
                     " SELECT sa.entry_id, sb.entry_id"
                     " FROM sna.entries sa JOIN snb.entries sb ON sa.path = sb.path")
        conn.execute("INSERT INTO rem_ids(id)"
                     " SELECT sa.entry_id FROM sna.entries sa"
                     " WHERE NOT EXISTS (SELECT 1 FROM snb.entries sb WHERE sb.path = sa.path)")
        conn.execute("INSERT INTO add_ids(id)"
                     " SELECT sb.entry_id FROM snb.entries sb"
                     " WHERE NOT EXISTS (SELECT 1 FROM sna.entries sa WHERE sa.path = sb.path)")
        conn.execute("CREATE INDEX ix_pairs_aid ON pairs(aid)")

        # 2. B 侧紧凑副本（顺序扫一遍，后续探测全是热页）
        conn.executescript(
            "CREATE TEMP TABLE bside(id INTEGER PRIMARY KEY, path TEXT NOT NULL,"
            " ptype TEXT, size INTEGER, mtime INTEGER, hash TEXT, hstate TEXT,"
            " pnorm TEXT, depth INTEGER);")
        conn.execute("INSERT INTO bside(id, path, ptype, size, mtime, hash, hstate, pnorm, depth)"
                     ' SELECT entry_id, path, "type", size_bytes, mtime_ns, hash_hex,'
                     " hash_state, path_norm, depth FROM snb.entries")

        # 3. matched 分类落库（identical 不落库）
        conn.execute(
            f"""
            {_INSERT_COLS}
            SELECT {cls}, sa.path, sa.path_norm, sa.depth,
                   sa.entry_id, bs.id,
                   sa.size_bytes, bs.size,
                   sa.mtime_ns, bs.mtime,
                   sa."type", bs.ptype,
                   CASE WHEN ?1 THEN sa.hash_hex END,
                   CASE WHEN ?1 THEN bs.hash END,
                   NULL
            FROM sna.entries sa
            CROSS JOIN pairs p INDEXED BY ix_pairs_aid ON p.aid = sa.entry_id
            CROSS JOIN bside bs ON bs.id = p.bid
            WHERE {cls} <> 'identical'
            """, (h,))

        # 4. removed（顺序扫 A）与 added（bside 热页）
        conn.execute(
            f"""
            {_INSERT_COLS}
            SELECT 'removed', sa.path, sa.path_norm, sa.depth,
                   sa.entry_id, NULL,
                   sa.size_bytes, NULL,
                   sa.mtime_ns, NULL,
                   sa."type", NULL,
                   CASE WHEN ?1 AND sa.hash_hex IS NOT NULL
                             AND sa.hash_state = 'full' THEN sa.hash_hex END,
                   NULL, NULL
            FROM sna.entries sa
            CROSS JOIN rem_ids r ON r.id = sa.entry_id
            """, (h,))
        conn.execute(
            f"""
            {_INSERT_COLS}
            SELECT 'added', bs.path, bs.pnorm, bs.depth,
                   NULL, bs.id,
                   NULL, bs.size,
                   NULL, bs.mtime,
                   NULL, bs.ptype,
                   NULL,
                   CASE WHEN ?1 AND bs.hash IS NOT NULL
                             AND bs.hstate = 'full' THEN bs.hash END,
                   bs.path
            FROM bside bs
            CROSS JOIN add_ids r ON r.id = bs.id
            """, (h,))
        conn.commit()

        # 5. 计数：已落库各类走 GROUP BY；identical = 对总数 − matched 已落库之和
        for ct, n in conn.execute(
                "SELECT change_type, COUNT(*) FROM diff_entries GROUP BY change_type"):
            counts[ct] = n
        pairs_total = conn.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
        matched_stored = (counts["type_changed"] + counts["size_changed"]
                          + counts["mtime_changed"] + (counts["content_changed"] or 0))
        counts["identical"] = pairs_total - matched_stored

        conn.executescript(
            "DROP INDEX ix_pairs_aid;"
            "DROP TABLE pairs;"
            "DROP TABLE rem_ids;"
            "DROP TABLE add_ids;"
            "DROP TABLE bside;")
    finally:
        try:
            conn.execute("DETACH DATABASE sna")
            conn.execute("DETACH DATABASE snb")
        except sqlite3.Error:
            pass
    return counts


def _materialize_python(conn: sqlite3.Connection, db_a: Path, db_b: Path,
                        hash_enabled: bool) -> dict:
    """旧 Python 逐行归并（path_norm 缺失时的回退路径，语义冻结）。"""
    counts = {k: 0 for k in _COUNTED}
    counts["content_changed"] = 0 if hash_enabled else None
    counts["moved_or_renamed"] = 0 if hash_enabled else None
    it_a = _iter_entries(db_a)
    it_b = _iter_entries(db_b)
    ra = next(it_a, None)
    rb = next(it_b, None)
    buf: "list[tuple]" = []

    def flush() -> None:
        if buf:
            conn.executemany(
                "INSERT INTO diff_entries(change_type,path,path_norm,depth,a_entry_id,b_entry_id,"
                "a_size,b_size,a_mtime_ns,b_mtime_ns,a_type,b_type,a_hash,b_hash,b_path)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buf)
            buf.clear()

    while ra is not None or rb is not None:
        if rb is None or (ra is not None and ra[0] < rb[0]):      # 仅 A 有
            counts["removed"] += 1
            buf.append(("removed", ra[0], ra[2] or ra[0].casefold(), ra[3],
                        ra[1], None, ra[5], None, ra[6], None, ra[4], None,
                        ra[7] if hash_enabled and _hash_usable(ra) else None,
                        None, None))
            ra = next(it_a, None)
        elif ra is None or rb[0] < ra[0]:                          # 仅 B 有
            counts["added"] += 1
            buf.append(("added", rb[0], rb[2] or rb[0].casefold(), rb[3],
                        None, rb[1], None, rb[5], None, rb[6], None, rb[4],
                        None,
                        rb[7] if hash_enabled and _hash_usable(rb) else None,
                        rb[0]))
            rb = next(it_b, None)
        else:                                                      # 两侧都有
            ct = _classify(ra, rb, hash_enabled)
            counts[ct] += 1
            if ct != "identical":
                buf.append((ct, ra[0], ra[2] or ra[0].casefold(), ra[3],
                            ra[1], rb[1], ra[5], rb[5], ra[6], rb[6], ra[4], rb[4],
                            ra[7] if hash_enabled else None,
                            rb[7] if hash_enabled else None, None))
            ra = next(it_a, None)
            rb = next(it_b, None)
        if len(buf) >= _BATCH_SIZE:
            flush()
            conn.commit()
    flush()
    conn.commit()
    return counts


def _all_path_norm_present(db: Path) -> bool:
    """entries.path_norm 是否全部非空非 ''（SQL 快路径的前提）。"""
    conn = open_snapshot(db)
    try:
        row = conn.execute(
            "SELECT 1 FROM entries WHERE path_norm IS NULL OR path_norm = '' LIMIT 1"
        ).fetchone()
        return row is None
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
    hash_enabled = opts["hash"] != "none"

    out_db.parent.mkdir(parents=True, exist_ok=True)
    # 未封库残留（上次运行崩溃）一律删除重建
    for stale in (out_db, Path(str(out_db) + "-wal"), Path(str(out_db) + "-shm")):
        stale.unlink(missing_ok=True)

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
            " a_type TEXT, b_type TEXT,"
            " a_hash TEXT, b_hash TEXT, b_path TEXT);"
        )

        if _all_path_norm_present(db_a) and _all_path_norm_present(db_b):
            counts = _materialize_sql(conn, db_a, db_b, hash_enabled)
        else:
            counts = _materialize_python(conn, db_a, db_b, hash_enabled)

        if hash_enabled:
            _pair_moved(conn, counts)

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
