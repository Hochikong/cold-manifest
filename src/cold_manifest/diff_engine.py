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
- identical 默认不落库只计数（show_identical=true 时 identical 也落 diff_entries，
  结果库会随相同条目数膨胀——大库慎用）；hash=none 时 content_changed /
  moved_or_renamed 恒为 NULL（不造假）。
- 按 path 精确 join：两个快照库各按 uq_entries_path 索引流式归并，内存 O(1)。

口径开关（§6.3 options，全部进 options_hash，各组合天然隔离、各自幂等复用）：
- case_insensitive=true：按 path_norm（casefold）配对而非精确 path。要求两侧
  entries.path_norm 全部非空非 ''，否则抛 DiffError（路由层转 400，提示该快照
  不支持大小写不敏感比对）。实现走 Python 归并（按 path_norm 排序的流式
  分组配对，同组内按 path 序一对一配对、多余拷贝归 removed/added），性能弱于
  SQL 快路径——大库 + case_insensitive 预期明显变慢。
- ignore_mtime=true：mtime 差异不再判 mtime_changed（无其他差异归 identical）。
  与 hash 组合：hash 不同 → content_changed（不受影响）；hash 相同 → 直接
  identical（mtime 被忽略）。
- ignore_size=true：大小差异不再判 size_changed。hash 分支本就忽略 size，
  因此只影响非 hash 回退分支。
- show_identical=true：identical 条目也落 diff_entries（默认只计数）。
  与 hash 组合：hash 相同 → identical 落库。
- evidence / evidence_level：物化完成时写入 diff_meta（evidence_level /
  evidence_json）。content_changed / moved_or_renamed 的证据为 "hash"（hash
  启用且两侧快照各至少有一条 full 哈希）或 "unavailable"；identical 的证据
  在有哈希证据时为 "hash" 否则 "size+mtime"；evidence_level 为本次对比整体
  依据："hash" 或 "size+mtime"。旧结果库无这两个键，read_evidence 返回
  (None, None)，由调用方回退展示。

写入风格与导入一致：journal_mode=OFF、批量事务、封库时建 DIFF_DDL 索引。
"""

import hashlib
import itertools
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .catalog import connect_catalog, snapshot_path
from .db import file_uri, open_snapshot
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
    evidence_level: "str | None" = None          # "hash" | "size+mtime"
    evidence: "dict | None" = None               # 各分类的判定依据
    hints: "list[str]" = field(default_factory=list)   # 平台提示等非结论性说明
    reused: bool = False
    elapsed_s: float = 0.0


def canonical_options(options: "dict | None") -> dict:
    """选项白名单（§6.3 diff 口径开关）。

    - hash: "none" | "sha256"
    - case_insensitive / ignore_mtime / ignore_size / show_identical: bool（默认 false）

    未知键或非法值抛 DiffError（路由层转 400）——静默接受会让非 canonical
    options 污染幂等键，产生永不复用的重复 diff 库。全部开关进入 options_hash，
    因此每种组合的 diff_id 天然隔离、各自幂等复用。取值 false 的布尔开关从
    canonical 形式中剔除（false 与缺省同键）。depth 是查询期过滤，不进 options。
    """
    opts = dict(options or {})
    allowed = {"hash", "case_insensitive", "ignore_mtime", "ignore_size", "show_identical"}
    unknown = set(opts) - allowed
    if unknown:
        raise DiffError(f"不支持的 diff 选项：{sorted(unknown)}"
                        f"（支持 {sorted(allowed)}）")
    algo = opts.get("hash", "none")
    if algo not in ("none", "sha256"):
        raise DiffError(f"不支持的 hash 选项值：{algo!r}（允许 'none' / 'sha256'）")
    opts["hash"] = algo
    for key in ("case_insensitive", "ignore_mtime", "ignore_size", "show_identical"):
        val = opts.get(key, False)
        if not isinstance(val, bool):
            raise DiffError(f"选项 {key} 需为布尔值，得到 {val!r}")
        if val:
            opts[key] = True
        else:
            opts.pop(key, None)
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
    try:
        conn = open_snapshot(db)
    except sqlite3.Error as e:
        raise DiffError(
            f"无法打开快照库：{snapshot_id}（{db}）——路径含 .. 段或文件损坏/"
            "无法打开，请检查路径与文件完整性") from e
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='status'").fetchone()
        if not row or row[0] != "sealed":
            raise DiffError(f"快照未封库，不能参与 diff：{snapshot_id}")
    except sqlite3.Error as e:
        raise DiffError(
            f"无法读取快照库：{snapshot_id}（{db}）——路径含 .. 段或文件损坏/"
            "无法打开，请检查路径与文件完整性") from e
    finally:
        conn.close()
    return db


def _iter_entries(db: Path, order: str = "path"):
    """按指定顺序流式产出 (path, entry_id, path_norm, depth, type, size, mtime,
    hash_hex, hash_state)。

    order="path"（默认）依赖封库时建的 uq_entries_path 索引做索引扫描，不排序、
    不整表载入；order="path_norm" 需 SQLite 临时排序（case_insensitive 归并专用）。
    """
    conn = open_snapshot(db)
    try:
        order_sql = "path_norm, path" if order == "path_norm" else "path"
        cur = conn.execute(
            "SELECT path, entry_id, path_norm, depth, type, size_bytes, mtime_ns,"
            f" hash_hex, hash_state FROM entries ORDER BY {order_sql}"
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


def _classify(ra, rb, hash_enabled: bool, ignore_mtime: bool = False,
              ignore_size: bool = False) -> str:
    if ra[4] != rb[4]:
        return "type_changed"
    if hash_enabled and _hash_usable(ra) and _hash_usable(rb):
        if ra[7] != rb[7]:
            return "content_changed"      # 同尺寸同 mtime 改写也逃不掉
        if not ignore_mtime and ra[6] != rb[6]:
            return "mtime_changed"
        return "identical"
    if not ignore_size and ra[5] != rb[5]:
        return "size_changed"
    if not ignore_mtime and ra[6] != rb[6]:
        return "mtime_changed"
    return "identical"


def _read_existing_status(db: Path) -> "str | None":
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(file_uri(db, immutable=False), uri=True)
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

    :hashp / :ig_s / :ig_m 参数为 1/0（hash 是否启用、ignore_size、ignore_mtime）。
    NULL 比较统一用 IS / IS NOT——Python 的 None != None 为 False（视为相等），
    IS 语义一致。
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
  WHEN :hashp AND {A}.hash_hex IS NOT NULL AND {A}.hash_state = 'full'
       AND {B_hash} IS NOT NULL AND {B_state} = 'full'
    THEN CASE WHEN {A}.hash_hex IS NOT {B_hash} THEN 'content_changed'
              WHEN NOT :ig_m AND {A}.mtime_ns IS NOT {B_mtime} THEN 'mtime_changed'
              ELSE 'identical' END
  WHEN NOT :ig_s AND {A}.size_bytes IS NOT {B_size} THEN 'size_changed'
  WHEN NOT :ig_m AND {A}.mtime_ns IS NOT {B_mtime} THEN 'mtime_changed'
  ELSE 'identical'
END"""


_BSIDE_COLS = {"entry_id": "bs.id", "type": "bs.ptype", "size": "bs.size",
               "mtime": "bs.mtime", "hash": "bs.hash", "state": "bs.hstate"}


_INSERT_COLS = (
    "INSERT INTO diff_entries(change_type,path,path_norm,depth,a_entry_id,b_entry_id,"
    "a_size,b_size,a_mtime_ns,b_mtime_ns,a_type,b_type,a_hash,b_hash,b_path)"
)


def _materialize_sql(conn: sqlite3.Connection, db_a: Path, db_b: Path,
                     hash_enabled: bool, ignore_mtime: bool = False,
                     ignore_size: bool = False, show_identical: bool = False) -> dict:
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
    ig_s, ig_m = (1 if ignore_size else 0), (1 if ignore_mtime else 0)
    cls = _cls_expr("sa", "bs", _BSIDE_COLS)
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA cache_size=-65536")
    try:
        try:
            conn.execute("ATTACH DATABASE ? AS sna",
                         (file_uri(db_a, immutable=True),))
            conn.execute("ATTACH DATABASE ? AS snb",
                         (file_uri(db_b, immutable=True),))
        except sqlite3.Error as e:
            raise DiffError(
                f"无法 ATTACH 快照库：{db_a} / {db_b}——"
                f"路径含 .. 段或无法打开（{e}）；请检查路径与文件完整性") from e

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

        # 3. matched 分类落库（identical 默认不落库，show_identical 时全落）
        where = "" if show_identical else f"WHERE {cls} <> 'identical'"
        conn.execute(
            f"""
            {_INSERT_COLS}
            SELECT {cls}, sa.path, sa.path_norm, sa.depth,
                   sa.entry_id, bs.id,
                   sa.size_bytes, bs.size,
                   sa.mtime_ns, bs.mtime,
                   sa."type", bs.ptype,
                   CASE WHEN :hashp THEN sa.hash_hex END,
                   CASE WHEN :hashp THEN bs.hash END,
                   NULL
            FROM sna.entries sa
            CROSS JOIN pairs p INDEXED BY ix_pairs_aid ON p.aid = sa.entry_id
            CROSS JOIN bside bs ON bs.id = p.bid
            {where}
            """, {"hashp": h, "ig_s": ig_s, "ig_m": ig_m})

        # 4. removed（顺序扫 A）与 added（bside 热页）
        conn.execute(
            f"""
            {_INSERT_COLS}
            SELECT 'removed', sa.path, sa.path_norm, sa.depth,
                   sa.entry_id, NULL,
                   sa.size_bytes, NULL,
                   sa.mtime_ns, NULL,
                   sa."type", NULL,
                   CASE WHEN :hashp AND sa.hash_hex IS NOT NULL
                             AND sa.hash_state = 'full' THEN sa.hash_hex END,
                   NULL, NULL
            FROM sna.entries sa
            CROSS JOIN rem_ids r ON r.id = sa.entry_id
            """, {"hashp": h})
        conn.execute(
            f"""
            {_INSERT_COLS}
            SELECT 'added', bs.path, bs.pnorm, bs.depth,
                   NULL, bs.id,
                   NULL, bs.size,
                   NULL, bs.mtime,
                   NULL, bs.ptype,
                   NULL,
                   CASE WHEN :hashp AND bs.hash IS NOT NULL
                             AND bs.hstate = 'full' THEN bs.hash END,
                   bs.path
            FROM bside bs
            CROSS JOIN add_ids r ON r.id = bs.id
            """, {"hashp": h})
        conn.commit()

        # 5. 计数：已落库各类走 GROUP BY；identical 在默认（不落库）模式下
        # 用算术：identical = 对总数 − matched 已落库各类之和
        for ct, n in conn.execute(
                "SELECT change_type, COUNT(*) FROM diff_entries GROUP BY change_type"):
            counts[ct] = n
        if show_identical:
            counts["identical"] = counts.get("identical", 0)
        else:
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
                        hash_enabled: bool, ignore_mtime: bool = False,
                        ignore_size: bool = False,
                        show_identical: bool = False) -> dict:
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
            ct = _classify(ra, rb, hash_enabled, ignore_mtime, ignore_size)
            counts[ct] += 1
            if ct != "identical" or show_identical:
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


def _materialize_python_ci(conn: sqlite3.Connection, db_a: Path, db_b: Path,
                           hash_enabled: bool, ignore_mtime: bool = False,
                           ignore_size: bool = False,
                           show_identical: bool = False) -> dict:
    """case_insensitive 归并：按 path_norm（casefold）分组配对（§6.3）。

    两侧各按 (path_norm, path) 排序流式分组；path_norm 相同的组内按 path 序
    一对一配对，多出的拷贝归 removed / added。物化前 materialize_diff 已保证
    两侧 path_norm 全部非空非 ''（否则抛 DiffError，不支持降级）。
    落库行的 path 取 A 侧原路径（added/removed 取各自侧）；moved_or_renamed
    配对沿用 _pair_moved（哈希相等，与大小写无关）。
    """
    counts = {k: 0 for k in _COUNTED}
    counts["content_changed"] = 0 if hash_enabled else None
    counts["moved_or_renamed"] = 0 if hash_enabled else None
    ga = itertools.groupby(_iter_entries(db_a, order="path_norm"), key=lambda r: r[2])
    gb = itertools.groupby(_iter_entries(db_b, order="path_norm"), key=lambda r: r[2])
    cur_a = next(ga, None)
    cur_b = next(gb, None)
    buf: "list[tuple]" = []

    def flush() -> None:
        if buf:
            conn.executemany(
                "INSERT INTO diff_entries(change_type,path,path_norm,depth,a_entry_id,b_entry_id,"
                "a_size,b_size,a_mtime_ns,b_mtime_ns,a_type,b_type,a_hash,b_hash,b_path)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", buf)
            buf.clear()

    while cur_a is not None or cur_b is not None:
        if cur_b is None or (cur_a is not None and cur_a[0] < cur_b[0]):
            for ra in cur_a[1]:                                    # 仅 A 有该折叠名
                counts["removed"] += 1
                buf.append(("removed", ra[0], ra[2], ra[3],
                            ra[1], None, ra[5], None, ra[6], None, ra[4], None,
                            ra[7] if hash_enabled and _hash_usable(ra) else None,
                            None, None))
            cur_a = next(ga, None)
        elif cur_a is None or cur_b[0] < cur_a[0]:                 # 仅 B 有该折叠名
            for rb in cur_b[1]:
                counts["added"] += 1
                buf.append(("added", rb[0], rb[2], rb[3],
                            None, rb[1], None, rb[5], None, rb[6], None, rb[4],
                            None,
                            rb[7] if hash_enabled and _hash_usable(rb) else None,
                            rb[0]))
            cur_b = next(gb, None)
        else:                                                      # 两侧同折叠名
            rows_a = list(cur_a[1])
            rows_b = list(cur_b[1])
            for ra, rb in zip(rows_a, rows_b):
                ct = _classify(ra, rb, hash_enabled, ignore_mtime, ignore_size)
                counts[ct] += 1
                if ct != "identical" or show_identical:
                    buf.append((ct, ra[0], ra[2], ra[3],
                                ra[1], rb[1], ra[5], rb[5], ra[6], rb[6],
                                ra[4], rb[4],
                                ra[7] if hash_enabled else None,
                                rb[7] if hash_enabled else None, None))
            for ra in rows_a[len(rows_b):]:                        # A 侧多余拷贝
                counts["removed"] += 1
                buf.append(("removed", ra[0], ra[2], ra[3],
                            ra[1], None, ra[5], None, ra[6], None, ra[4], None,
                            ra[7] if hash_enabled and _hash_usable(ra) else None,
                            None, None))
            for rb in rows_b[len(rows_a):]:                        # B 侧多余拷贝
                counts["added"] += 1
                buf.append(("added", rb[0], rb[2], rb[3],
                            None, rb[1], None, rb[5], None, rb[6], None, rb[4],
                            None,
                            rb[7] if hash_enabled and _hash_usable(rb) else None,
                            rb[0]))
            cur_a = next(ga, None)
            cur_b = next(gb, None)
        if len(buf) >= _BATCH_SIZE:
            flush()
            conn.commit()
    flush()
    conn.commit()
    return counts


def _has_full_hash(db: Path) -> bool:
    """该快照是否至少存在一条可用于内容等值比对的 full 哈希（evidence 依据）。"""
    conn = open_snapshot(db)
    try:
        row = conn.execute(
            "SELECT 1 FROM entries WHERE hash_hex IS NOT NULL AND hash_state='full' LIMIT 1"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _build_evidence(db_a: Path, db_b: Path, hash_enabled: bool) -> "tuple[str, dict]":
    """(evidence_level, evidence)：本次对比各分类结论的判定依据（§6.3）。"""
    content_ev = ("hash" if hash_enabled and _has_full_hash(db_a) and _has_full_hash(db_b)
                  else "unavailable")
    evidence = {
        "content_changed": content_ev,
        "moved_or_renamed": content_ev,
        "size_changed": "size",
        "mtime_changed": "mtime",
        "type_changed": "type",
        "identical": "hash" if content_ev == "hash" else "size+mtime",
    }
    return ("hash" if content_ev == "hash" else "size+mtime"), evidence


_HINT_CASE_INSENSITIVE = (
    "两侧快照至少一方来自 Windows（文件名大小写不敏感）；若源盘上存在仅大小写"
    "不同的改名（如 a.txt → A.TXT），会显示为 1 条删除 + 1 条新增。"
    "如需正确配对，可加 case_insensitive 重跑。"
)


def _snapshot_os_platform(db: Path) -> "str | None":
    """快照 meta 的采集平台标记（os_platform）；旧快照无该键返回 None。"""
    conn = open_snapshot(db)
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='os_platform'").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _build_hints(opts: dict, db_a: Path, db_b: Path) -> "list[str]":
    """平台相关提示：未启用 case_insensitive 且任一侧来自 Windows 时提示（P1-4）。

    旧快照无 os_platform 键时不提示（不猜测平台）。
    """
    if opts.get("case_insensitive", False):
        return []
    platforms = {_snapshot_os_platform(db_a), _snapshot_os_platform(db_b)}
    if "win32" not in platforms:
        return []
    return [_HINT_CASE_INSENSITIVE]


def _read_hints(db: Path) -> "list[str]":
    """读 diff_meta 的 hints_json；旧结果库无该键返回 []。"""
    if not db.is_file():
        return []
    conn = sqlite3.connect(file_uri(db, immutable=False), uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM diff_meta WHERE key='hints_json'").fetchone()
        if not row:
            return []
        try:
            hints = json.loads(row[0])
        except json.JSONDecodeError:
            return []
        return hints if isinstance(hints, list) else []
    finally:
        conn.close()


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
        ev_level, ev = _read_evidence(out_db)
        hints = _read_hints(out_db)
        if ev is None:   # 旧库无 evidence 键：按 options/counts 推导（best effort）
            ev_level = "hash" if counts.get("content_changed") is not None else "size+mtime"
            content_ev = "hash" if ev_level == "hash" else "unavailable"
            ev = {"content_changed": content_ev, "moved_or_renamed": content_ev,
                  "size_changed": "size", "mtime_changed": "mtime",
                  "type_changed": "type",
                  "identical": "hash" if content_ev == "hash" else "size+mtime"}
        return DiffResult(diff_id=did, a=a, b=b, db_path=out_db, options_json=ojson,
                          options_hash=ohash, counts=counts, evidence_level=ev_level,
                          evidence=ev, hints=hints, reused=True,
                          elapsed_s=time.monotonic() - t0)

    try:
        return _materialize_new(data_root, a, b, opts, ohash, ojson, did, out_db, t0)
    except DiffError as e:
        # diff_runs 记 error（可读失败，不静默）：尽力而为，catalog 打不开则跳过
        try:
            cat = connect_catalog(data_root)
            try:
                cat.execute(
                    "INSERT OR REPLACE INTO diff_runs(diff_id, a, b, options_hash, options_json,"
                    " created_at, duration_ms, status, summary_json, result_path)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (did, a, b, ohash, ojson,
                     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     int((time.monotonic() - t0) * 1000), "error",
                     json.dumps({"error": str(e)}, ensure_ascii=False), None))
                cat.commit()
            finally:
                cat.close()
        except Exception:
            pass
        raise


def _materialize_new(data_root: Path, a: str, b: str, opts: dict, ohash: str,
                     ojson: str, did: str, out_db: Path, t0: float) -> DiffResult:
    """materialize_diff 的实际物化路径（失败由外层记 diff_runs error）。"""
    db_a = _sealed_snapshot(data_root, a)
    db_b = _sealed_snapshot(data_root, b)
    hash_enabled = opts["hash"] != "none"
    ci = opts.get("case_insensitive", False)
    ig_m = opts.get("ignore_mtime", False)
    ig_s = opts.get("ignore_size", False)
    show_id = opts.get("show_identical", False)
    db_b = _sealed_snapshot(data_root, b)

    # case_insensitive 前置校验：path_norm 缺失即报错（不静默降级到精确 path）
    if ci:
        for sid, db in ((a, db_a), (b, db_b)):
            if not _all_path_norm_present(db):
                raise DiffError(
                    f"快照 {sid} 存在 path_norm 缺失的条目，"
                    "不支持大小写不敏感比对（请用新版采集重建该快照）")

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

        if ci:
            counts = _materialize_python_ci(conn, db_a, db_b, hash_enabled,
                                            ig_m, ig_s, show_id)
        elif _all_path_norm_present(db_a) and _all_path_norm_present(db_b):
            counts = _materialize_sql(conn, db_a, db_b, hash_enabled, ig_m, ig_s, show_id)
        else:
            counts = _materialize_python(conn, db_a, db_b, hash_enabled, ig_m, ig_s, show_id)

        if hash_enabled:
            _pair_moved(conn, counts)

        evidence_level, evidence = _build_evidence(db_a, db_b, hash_enabled)
        hints = _build_hints(opts, db_a, db_b)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta_rows = [
            ("a", a), ("b", b), ("options_json", ojson), ("options_hash", ohash),
            ("created_at", now), ("status", "done"),
            ("summary_json", json.dumps(counts, sort_keys=True)),
            ("evidence_level", evidence_level),
            ("evidence_json", json.dumps(evidence, sort_keys=True)),
            ("hints_json", json.dumps(hints, ensure_ascii=False)),
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
                      options_hash=ohash, counts=counts, evidence_level=evidence_level,
                      evidence=evidence, hints=hints, reused=False, elapsed_s=elapsed)


def _read_summary(db: Path) -> dict:
    conn = sqlite3.connect(file_uri(db, immutable=False), uri=True)
    try:
        row = conn.execute("SELECT value FROM diff_meta WHERE key='summary_json'").fetchone()
        return json.loads(row[0]) if row else {}
    finally:
        conn.close()


def _read_evidence(db: Path) -> "tuple[str | None, dict | None]":
    """读 diff_meta 的 evidence_level / evidence_json；旧库返回 (None, None)。"""
    if not db.is_file():
        return None, None
    conn = sqlite3.connect(file_uri(db, immutable=False), uri=True)
    try:
        try:
            rows = dict(conn.execute(
                "SELECT key, value FROM diff_meta WHERE key IN"
                " ('evidence_level', 'evidence_json')").fetchall())
        except sqlite3.Error:
            return None, None
    finally:
        conn.close()
    if "evidence_level" not in rows or "evidence_json" not in rows:
        return None, None
    try:
        return rows["evidence_level"], json.loads(rows["evidence_json"])
    except json.JSONDecodeError:
        return None, None


def iter_diff_csv(db: Path, depth: "int | None" = None):
    """流式产出 CSV 行 (category, path, type, size_a, size_b, mtime_a, mtime_b)。

    type 取 A 侧（缺失取 B 侧）；type_changed 两侧类型都存在且不同时记 'a->b'
    （类型直接读 diff_entries.a_type/b_type，物化时已填充）。
    """
    main = sqlite3.connect(file_uri(db, immutable=False), uri=True)
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
