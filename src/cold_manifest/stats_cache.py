"""stats 预计算（P2-A）：封库期把 /stats 聚合算好落表，查询直读。

10M 压测（docs/压测-10M.md）唯一超标项：stats 端点实时聚合 6–9.6s、RSS 1.58GiB。
方案：seal_snapshot 末尾把与实时实现逐桶等值的结果写入 stats_precomputed
（key TEXT PRIMARY KEY, value_json TEXT，DDL 见 schema.py），查询端点优先直读；
旧快照无该表 → 回退实时聚合（接口零破坏）；存量库用 `cldm build-stats` 幂等补建。

注意：快照封库后 entries 不可变，预计算结果不会过期；按需哈希只回填 hash 列，
不影响任何 stats 聚合口径。
"""

import json
import sqlite3
from typing import Any

# 大小直方图桶边界（累计计数后差分）——与实时实现共用同一份口径
SIZE_BUCKETS: "list[tuple[int, str]]" = [
    (1024, "<=1KB"), (1048576, "1KB-1MB"), (10485760, "1MB-10MB"),
    (104857600, "10MB-100MB"), (1073741824, "100MB-1GB"),
    (10737418240, "1GB-10GB"), (107374182400, "10GB-100GB"),
    (1099511627776, "100GB-1TB"), (9223372036854775807, ">=1TB"),
]

# stats_precomputed 中的合法 key；查询端校验齐备才直读（缺任一 → 回退实时）
STATS_KEYS = (
    "ext_top_by_bytes", "ext_top_by_count", "size_histogram",
    "depth_histogram", "zero_byte_count", "top_files",
)


def stats_precomputed_available(conn: sqlite3.Connection) -> bool:
    """快照库是否已有 stats_precomputed 表（旧库兼容探测，对齐 PRAGMA table_info 风格）。"""
    rows = conn.execute("PRAGMA table_info(stats_precomputed)").fetchall()
    return bool(rows)


def compute_stats(conn: sqlite3.Connection) -> dict:
    """实时聚合（与 routes_snapshots.stats 原实现逐字段等值；不含 snapshot_id）。

    封库预计算与查询回退共用本函数，保证两种路径结果严格一致。
    连接可能未设 row_factory（封库路径），游标统一自带 sqlite3.Row。
    """
    def q(sql: str, params: tuple = ()) -> list:
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        return cur.execute(sql, params).fetchall()

    # 扩展名分布：一次覆盖索引扫描同时取 count 与 sum，Python 侧排两个榜
    ext_rows = q(
        "SELECT ext, COUNT(*) AS n, SUM(size_bytes) AS sz FROM entries"
        " WHERE ext <> '' GROUP BY ext"
    )
    ext_top_by_bytes = sorted(
        ({"ext": r["ext"], "total_bytes": r["sz"] or 0} for r in ext_rows),
        key=lambda x: -x["total_bytes"])[:20]
    ext_top_by_count = sorted(
        ({"ext": r["ext"], "count": r["n"]} for r in ext_rows),
        key=lambda x: -x["count"])[:20]

    # 大小直方图：按桶边界做累计范围计数（idx_entries_size 覆盖），差分得各桶
    edges = [edge for edge, _ in SIZE_BUCKETS]
    cum: "list[int]" = []
    cum_bytes: "list[int]" = []
    for edge in edges:
        r = q(
            "SELECT COUNT(*) AS c, COALESCE(SUM(size_bytes), 0) AS s"
            " FROM entries WHERE size_bytes <= ?",
            (edge,),
        )[0]
        cum.append(r["c"])
        cum_bytes.append(r["s"])
    buckets = []
    prev_n = prev_b = 0
    for i, (_, label) in enumerate(SIZE_BUCKETS):
        buckets.append({"label": label, "count": cum[i] - prev_n,
                        "total_bytes": cum_bytes[i] - prev_b})
        prev_n, prev_b = cum[i], cum_bytes[i]

    # 深度直方图（仅文件行）
    depth_hist = q(
        "SELECT depth, COUNT(*) AS n FROM entries WHERE type = 'file' GROUP BY depth"
    )

    # 零字节与 top 50
    zero_count = q("SELECT COUNT(*) AS c FROM entries WHERE size_bytes = 0")[0]["c"]
    top_files = [
        {
            "entry_id": r["entry_id"], "path": r["path"], "name": r["name"],
            "size_bytes": r["size_bytes"],
            "mtime_ns": str(r["mtime_ns"]) if r["mtime_ns"] is not None else None,
        }
        for r in q(
            "SELECT entry_id, path, name, size_bytes, mtime_ns FROM entries"
            " WHERE type='file' ORDER BY size_bytes DESC LIMIT 50"
        )
    ]
    return {
        "ext_top_by_bytes": ext_top_by_bytes,
        "ext_top_by_count": ext_top_by_count,
        "size_histogram": buckets,
        "depth_histogram": [{"depth": d["depth"], "count": d["n"]} for d in depth_hist],
        "zero_byte_count": zero_count,
        "top_files": top_files,
    }


def build_stats_cache(conn: sqlite3.Connection) -> int:
    """封库期/补建：计算并落表 stats_precomputed（幂等，整体重建保证与 entries 一致）。

    返回写入的聚合段数（= len(STATS_KEYS)）。commit 由调用方负责。
    """
    result = compute_stats(conn)
    conn.execute("DROP TABLE IF EXISTS stats_precomputed")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stats_precomputed("
        " key TEXT PRIMARY KEY, value_json TEXT NOT NULL)"
    )
    for key in STATS_KEYS:
        conn.execute(
            "INSERT INTO stats_precomputed(key, value_json) VALUES (?, ?)",
            (key, json.dumps(result[key], ensure_ascii=False)),
        )
    return len(STATS_KEYS)


def load_precomputed_stats(conn: sqlite3.Connection) -> "dict | None":
    """直读预计算结果；表缺失或任一 key 缺失/损坏 → None（查询端回退实时聚合）。"""
    if not stats_precomputed_available(conn):
        return None
    try:
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        rows = cur.execute(
            "SELECT key, value_json FROM stats_precomputed").fetchall()
    except sqlite3.Error:
        return None
    data = {r["key"]: json.loads(r["value_json"]) for r in rows}
    if any(key not in data for key in STATS_KEYS):
        return None
    return {key: data[key] for key in STATS_KEYS}
