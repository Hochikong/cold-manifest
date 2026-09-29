"""snapshots 相关只读端点（§6.2）：列表/详情/stats/entries/tree/du/search/skipped。"""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from ..catalog import find_snapshot
from ..exporter import export_v1_csv_zip, iter_snapshot_csv
from .pagination import decode_cursor, encode_cursor
from .state import AppState, get_state

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])

_LIMIT_DEFAULT = 200
_LIMIT_MAX = 500

EntryType = Literal["file", "dir", "symlink", "other"]
SortKey = Literal["name", "size", "mtime"]

# 排序键表达式；NULL 参与比较会把行整体判 NULL 丢弃，故 size/mtime 用 COALESCE 归一
_SORT_EXPRS: "dict[str, tuple[str, bool]]" = {
    # (SQL 表达式, 是否文本列——文本需 COLLATE NOCASE 与 idx_entries_name 一致)
    "name": ("name", True),
    "size": ("COALESCE(size_bytes, -1)", False),
    "mtime": ("COALESCE(mtime_ns, -1)", False),
}


def _require_snapshot(state: AppState, snapshot_id: str) -> Any:
    """catalog 中存在该快照，否则 404。"""
    row = find_snapshot(state.catalog, snapshot_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"快照不存在：{snapshot_id}")
    return row


def _snap_db(request: Request, snapshot_id: str) -> Any:
    """快照库连接；catalog 已登记但库文件缺失同样 404。"""
    _require_snapshot(get_state(request), snapshot_id)
    try:
        return get_state(request).snapshot_db(snapshot_id)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"快照库文件缺失：{snapshot_id}") from None


def _rowget(row: Any) -> dict:
    return dict(row)


def _resolve_parent(conn: Any, parent_id: int) -> int:
    """parent_id=0 → root 条目（生产布局 entry_id=1, parent_id=0, depth=0）。

    顶层条目的 parent_id 是 root 的 entry_id 而非 0；直接把 0 下传 SQL
    会命中 root 行本身（幻影行）。无 root 行的库退回 0。
    """
    if parent_id != 0:
        return parent_id
    row = conn.execute(
        "SELECT entry_id FROM entries WHERE parent_id = 0 AND depth = 0").fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------- 列表 / 详情


@router.get("")
def list_snapshots(
    request: Request,
    volume_id: "str | None" = Query(default=None),
) -> dict:
    """快照列表（catalog 预计算统计直出 + volume/disk 概要）。"""
    state = get_state(request)
    sql = """
    SELECT s.snapshot_id, s.volume_id, s.batch_id, s.collected_at, s.status,
           s.hash_policy, s.file_count, s.dir_count, s.total_bytes, s.total_alloc,
           s.zero_byte_count, s.max_depth, s.skipped_count, s.host_path,
           v.disk_id, v.filesystem, v.label, v.capacity_bytes AS volume_capacity_bytes,
           d.physical_model, d.physical_serial, d.capacity_bytes AS disk_capacity_bytes
    FROM snapshots s
    JOIN volumes v ON v.volume_id = s.volume_id
    LEFT JOIN disks d ON d.disk_id = v.disk_id
    """
    params: "list[Any]" = []
    if volume_id:
        sql += " WHERE s.volume_id = ?"
        params.append(volume_id)
    sql += " ORDER BY s.volume_id, s.collected_at"
    items = [_rowget(r) for r in state.catalog.execute(sql, params)]
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------- 详情
# 注意：/{snapshot_id:path} 会吞掉带斜杠的子路径，必须最后注册（见文件底部）


# ---------------------------------------------------------------- stats


# 大小直方图桶边界（累计计数后差分）
_SIZE_BUCKETS: "list[tuple[int, str]]" = [
    (1024, "<=1KB"), (1048576, "1KB-1MB"), (10485760, "1MB-10MB"),
    (104857600, "10MB-100MB"), (1073741824, "100MB-1GB"),
    (10737418240, "1GB-10GB"), (107374182400, "10GB-100GB"),
    (1099511627776, "100GB-1TB"), (9223372036854775807, ">=1TB"),
]



@router.get("/{snapshot_id:path}/stats")
def snapshot_stats(snapshot_id: str, request: Request) -> dict:
    """扩展名分布（按大小/数量）、大小直方图、top 50 大文件、零字节计数、深度直方图。

    各聚合分别走覆盖索引（idx_entries_ext_size / idx_entries_size），
    避免对 3M 宽表行（path 文本）做全表扫描与临时排序（3M 行实测 <1s）。
    """
    conn = _snap_db(request, snapshot_id)

    # 扩展名分布：一次覆盖索引扫描同时取 count 与 sum，Python 侧排两个榜
    ext_rows = conn.execute(
        "SELECT ext, COUNT(*) AS n, SUM(size_bytes) AS sz FROM entries"
        " WHERE ext <> '' GROUP BY ext"
    ).fetchall()
    ext_top_by_bytes = sorted(
        ({"ext": r["ext"], "total_bytes": r["sz"] or 0} for r in ext_rows),
        key=lambda x: -x["total_bytes"])[:20]
    ext_top_by_count = sorted(
        ({"ext": r["ext"], "count": r["n"]} for r in ext_rows),
        key=lambda x: -x["count"])[:20]

    # 大小直方图：按桶边界做累计范围计数（idx_entries_size 覆盖），差分得各桶
    edges = [edge for edge, _ in _SIZE_BUCKETS]
    cum: "list[int]" = []
    cum_bytes: "list[int]" = []
    for edge in edges:
        r = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM entries WHERE size_bytes <= ?",
            (edge,),
        ).fetchone()
        cum.append(r[0])
        cum_bytes.append(r[1])
    buckets = []
    prev_n = prev_b = 0
    for i, (_, label) in enumerate(_SIZE_BUCKETS):
        buckets.append({"label": label, "count": cum[i] - prev_n,
                        "total_bytes": cum_bytes[i] - prev_b})
        prev_n, prev_b = cum[i], cum_bytes[i]

    # 深度直方图（仅文件行）：无 depth 索引，一趟分组；3M 行实测 ~0.5s
    depth_hist = conn.execute(
        "SELECT depth, COUNT(*) AS n FROM entries WHERE type = 'file' GROUP BY depth"
    ).fetchall()

    # 零字节与 top 50：idx_entries_size 范围扫描
    zero_count = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE size_bytes = 0").fetchone()[0]
    top_files = [
        {
            "entry_id": r["entry_id"], "path": r["path"], "name": r["name"],
            "size_bytes": r["size_bytes"], "mtime_ns": str(r["mtime_ns"]) if r["mtime_ns"] is not None else None,
        }
        for r in conn.execute(
            "SELECT entry_id, path, name, size_bytes, mtime_ns FROM entries"
            " WHERE type='file' ORDER BY size_bytes DESC LIMIT 50"
        )
    ]
    return {
        "snapshot_id": snapshot_id,
        "ext_top_by_bytes": ext_top_by_bytes,
        "ext_top_by_count": ext_top_by_count,
        "size_histogram": buckets,
        "depth_histogram": [{"depth": d["depth"], "count": d["n"]} for d in depth_hist],
        "zero_byte_count": zero_count,
        "top_files": top_files,
    }


# ---------------------------------------------------------------- entries


def _normalize_ext(ext: "str | None") -> "str | None":
    if ext is None or ext == "":
        return None
    ext = ext.lower()
    return ext if ext.startswith(".") else f".{ext}"


def _keyset_clause(key_expr: str, is_text: bool, op: str, id_col: str = "e.entry_id") -> str:
    coll = " COLLATE NOCASE" if is_text else ""
    return (f"({key_expr}{coll} {op} ?{coll}"
            f" OR ({key_expr}{coll} = ?{coll} AND {id_col} {op} ?))")


@router.get("/{snapshot_id:path}/entries")
def list_entries(
    snapshot_id: str,
    request: Request,
    parent_id: int = Query(default=0, ge=0),
    cursor: "str | None" = Query(default=None),
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
    sort: SortKey = Query(default="name"),
    order: Literal["asc", "desc"] = Query(default="asc"),
    type: "EntryType | None" = Query(default=None),
    ext: "str | None" = Query(default=None),
    min_size: "int | None" = Query(default=None, ge=0),
    max_size: "int | None" = Query(default=None, ge=0),
    q: "str | None" = Query(default=None, max_length=200),
) -> dict:
    """目录直接子项 keyset 分页（游标含排序键 + entry_id；不返回总数）。

    目录行携带 dir_rollup，前端无需二次请求即可显示递归大小/文件数。
    """
    conn = _snap_db(request, snapshot_id)
    parent_id = _resolve_parent(conn, parent_id)
    key_expr, is_text = _SORT_EXPRS[sort]
    desc = order == "desc"
    op = "<" if desc else ">"

    where = ["e.parent_id = ?"]
    params: "list[Any]" = [parent_id]
    if type:
        where.append("e.type = ?")
        params.append(type)
    if ext_norm := _normalize_ext(ext):
        where.append("e.ext = ?")
        params.append(ext_norm)
    if min_size is not None:
        where.append("e.size_bytes >= ?")
        params.append(min_size)
    if max_size is not None:
        where.append("e.size_bytes <= ?")
        params.append(max_size)
    if q:
        where.append(r"e.name LIKE ? ESCAPE '\'")
        params.append("%" + q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%")
    if cursor is not None:
        try:
            key_val, last_id = decode_cursor(cursor)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        where.append(_keyset_clause(key_expr, is_text, op))
        params += [key_val, key_val, last_id]

    order_sql = "DESC" if desc else "ASC"
    # 文本排序键必须与 keyset 比较同为 NOCASE，否则混合大小写翻页会静默丢行
    coll = " COLLATE NOCASE" if is_text else ""
    rows = conn.execute(
        f"""
        SELECT e.entry_id, e.name, e.type, e.size_bytes, e.allocated_bytes,
               e.mtime_ns, e.ext, e.hash_state, e.attrs,
               r.file_count AS r_file_count, r.dir_count AS r_dir_count,
               r.total_bytes AS r_total_bytes, r.total_allocated AS r_total_allocated,
               r.max_mtime_ns AS r_max_mtime_ns
        FROM entries e
        LEFT JOIN dir_rollup r ON r.entry_id = e.entry_id
        WHERE {' AND '.join(where)}
        ORDER BY {key_expr}{coll} {order_sql}, e.entry_id {order_sql}
        LIMIT ?
        """,
        [*params, limit + 1],
    ).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for r in rows:
        item = {
            "entry_id": r["entry_id"], "name": r["name"], "type": r["type"],
            "size_bytes": r["size_bytes"], "allocated_bytes": r["allocated_bytes"],
            "mtime_ns": str(r["mtime_ns"]) if r["mtime_ns"] is not None else None,
            "ext": r["ext"], "hash_state": r["hash_state"], "attrs": r["attrs"],
        }
        if r["type"] == "dir" and r["r_file_count"] is not None:
            item["rollup"] = {
                "file_count": r["r_file_count"], "dir_count": r["r_dir_count"],
                "total_bytes": r["r_total_bytes"], "total_allocated": r["r_total_allocated"],
                "max_mtime_ns": str(r["r_max_mtime_ns"]) if r["r_max_mtime_ns"] is not None else None,
            }
        items.append(item)

    parent = conn.execute(
        "SELECT entry_id, path, name, type FROM entries WHERE entry_id=?", (parent_id,)
    ).fetchone()
    counts = {"files": 0, "dirs": 0, "symlinks": 0, "others": 0}
    for c in conn.execute(
        "SELECT type, COUNT(*) AS n FROM entries WHERE parent_id=? GROUP BY type", (parent_id,)
    ):
        counts[{"file": "files", "dir": "dirs", "symlink": "symlinks"}.get(c["type"], "others")] = c["n"]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        if sort == "name":
            key_val = last["name"]
        elif sort == "size":
            key_val = last["size_bytes"] if last["size_bytes"] is not None else -1
        else:
            key_val = last["mtime_ns"] if last["mtime_ns"] is not None else -1
        next_cursor = encode_cursor(key_val, last["entry_id"])

    return {
        "snapshot_id": snapshot_id,
        "parent_id": parent_id,
        "parent_path": parent["path"] if parent else None,
        "direct_child_counts": counts,
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


# ---------------------------------------------------------------- tree / du


@router.get("/{snapshot_id:path}/tree")
def snapshot_tree(
    snapshot_id: str,
    request: Request,
    parent_id: int = Query(default=0, ge=0),
) -> dict:
    """单级子目录（携带 rollup）+ 直接文件数，供目录树懒加载。"""
    conn = _snap_db(request, snapshot_id)
    parent_id = _resolve_parent(conn, parent_id)
    dirs = [
        {
            "entry_id": r["entry_id"], "name": r["name"],
            "file_count": r["file_count"], "dir_count": r["dir_count"],
            "total_bytes": r["total_bytes"], "total_allocated": r["total_allocated"],
            "max_mtime_ns": str(r["max_mtime_ns"]) if r["max_mtime_ns"] is not None else None,
        }
        for r in conn.execute(
            """
            SELECT e.entry_id, e.name, r.file_count, r.dir_count,
                   r.total_bytes, r.total_allocated, r.max_mtime_ns
            FROM entries e JOIN dir_rollup r ON r.entry_id = e.entry_id
            WHERE e.parent_id = ? AND e.type = 'dir'
            ORDER BY e.name COLLATE NOCASE
            """,
            (parent_id,),
        )
    ]
    file_count = conn.execute(
        "SELECT COUNT(*) FROM entries WHERE parent_id = ? AND type = 'file'", (parent_id,)
    ).fetchone()[0]
    parent = conn.execute(
        "SELECT entry_id, path, name FROM entries WHERE entry_id = ?", (parent_id,)
    ).fetchone()
    return {
        "snapshot_id": snapshot_id,
        "parent_id": parent_id,
        "parent_path": parent["path"] if parent else None,
        "dirs": dirs,
        "direct_file_count": file_count,
    }


@router.get("/{snapshot_id:path}/du")
def snapshot_du(
    snapshot_id: str,
    request: Request,
    parent_id: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=_LIMIT_MAX),
) -> dict:
    """按大小降序的直接子项：目录用 dir_rollup.total_bytes，文件用 size_bytes。

    供 treemap / 磁盘占用排行；一律查 dir_rollup，不在请求期递归（§6.4）。
    """
    conn = _snap_db(request, snapshot_id)
    parent_id = _resolve_parent(conn, parent_id)
    rows = conn.execute(
        """
        SELECT e.entry_id, e.name, e.type, e.path,
               COALESCE(r.total_bytes, e.size_bytes) AS total_bytes
        FROM entries e
        LEFT JOIN dir_rollup r ON r.entry_id = e.entry_id
        WHERE e.parent_id = ? AND e.type IN ('file', 'dir')
          AND COALESCE(r.total_bytes, e.size_bytes) IS NOT NULL
        ORDER BY total_bytes DESC
        LIMIT ?
        """,
        (parent_id, limit),
    ).fetchall()
    return {
        "snapshot_id": snapshot_id,
        "parent_id": parent_id,
        "items": [
            {"entry_id": r["entry_id"], "name": r["name"], "type": r["type"],
             "path": r["path"], "total_bytes": r["total_bytes"]}
            for r in rows
        ],
    }


# ---------------------------------------------------------------- search / skipped


@router.get("/{snapshot_id:path}/search")
def search_entries(
    snapshot_id: str,
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    mode: Literal["prefix"] = Query(default="prefix"),
    type: "EntryType | None" = Query(default=None),
    ext: "str | None" = Query(default=None),
    min_size: "int | None" = Query(default=None, ge=0),
    max_size: "int | None" = Query(default=None, ge=0),
    cursor: "str | None" = Query(default=None),
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
) -> dict:
    """前缀搜索：走 idx_entries_name（NOCASE），keyset 分页（FTS 模式后续补）。"""
    conn = _snap_db(request, snapshot_id)
    op = ">"
    where = [r"name LIKE ? ESCAPE '\'"]
    prefix = q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    params: "list[Any]" = [prefix]
    if type:
        where.append("type = ?")
        params.append(type)
    if ext_norm := _normalize_ext(ext):
        where.append("ext = ?")
        params.append(ext_norm)
    if min_size is not None:
        where.append("size_bytes >= ?")
        params.append(min_size)
    if max_size is not None:
        where.append("size_bytes <= ?")
        params.append(max_size)
    if cursor is not None:
        try:
            key_val, last_id = decode_cursor(cursor)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        where.append(_keyset_clause("name", True, op, id_col="entry_id"))
        params += [key_val, key_val, last_id]

    rows = conn.execute(
        f"""
        SELECT entry_id, name, path, type, size_bytes, ext
        FROM entries
        WHERE {' AND '.join(where)}
        ORDER BY name COLLATE NOCASE, entry_id
        LIMIT ?
        """,
        [*params, limit + 1],
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_cursor(rows[-1]["name"], rows[-1]["entry_id"]) if has_more and rows else None
    )
    return {
        "snapshot_id": snapshot_id,
        "mode": mode,
        "items": [
            {"entry_id": r["entry_id"], "name": r["name"], "path": r["path"],
             "type": r["type"], "size_bytes": r["size_bytes"], "ext": r["ext"]}
            for r in rows
        ],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@router.get("/{snapshot_id:path}/skipped")
def list_skipped(
    snapshot_id: str,
    request: Request,
    cursor: "str | None" = Query(default=None),
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
    stage: "str | None" = Query(default=None),
    warning_type: "str | None" = Query(default=None),
) -> dict:
    """未采集项（skipped 表），按 (path, warning_type) 主键做 keyset 分页。"""
    conn = _snap_db(request, snapshot_id)
    where = ["1=1"]
    params: "list[Any]" = []
    if stage:
        where.append("stage = ?")
        params.append(stage)
    if warning_type:
        where.append("warning_type = ?")
        params.append(warning_type)
    if cursor is not None:
        try:
            obj_key, last_id = decode_cursor(cursor)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        if not (isinstance(obj_key, list) and len(obj_key) == 2):
            raise HTTPException(status_code=400, detail="cursor 排序键不匹配")
        p, w = obj_key
        where.append("(path > ? OR (path = ? AND warning_type > ?))")
        params += [p, p, w]
    rows = conn.execute(
        f"""
        SELECT path, warning_type, stage, detail FROM skipped
        WHERE {' AND '.join(where)}
        ORDER BY path, warning_type
        LIMIT ?
        """,
        [*params, limit + 1],
    ).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_cursor([rows[-1]["path"], rows[-1]["warning_type"]], 0)
        if has_more and rows else None
    )
    return {"snapshot_id": snapshot_id, "items": [_rowget(r) for r in rows],
            "next_cursor": next_cursor, "has_more": has_more}


# ---------------------------------------------------------------- 导出


@router.get("/{snapshot_id:path}/export")
def snapshot_export(
    snapshot_id: str,
    request: Request,
    format: Literal["csv", "v1_csv"] = Query(default="csv"),
) -> object:
    """快照导出：csv=entries 全表流式 CSV；v1_csv=v1-layout 三件套 zip（用后即删）。

    连接来自 AppState LRU 池，只借不关（池负责生命周期）。
    """
    conn = _snap_db(request, snapshot_id)
    safe_name = snapshot_id.replace("/", "_")

    if format == "csv":
        return StreamingResponse(
            iter_snapshot_csv(conn),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}_entries.csv"'},
        )

    # v1_csv：exporter 生成临时 zip → FileResponse，响应完成后后台清理
    zip_path = export_v1_csv_zip(conn, snapshot_id)
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"{safe_name}_v1.zip",
        background=BackgroundTask(zip_path.unlink),
    )


# ---------------------------------------------------------------- 详情（最后注册：:path 转换器会吞子路径）


@router.get("/{snapshot_id:path}")
def snapshot_detail(snapshot_id: str, request: Request) -> dict:
    """详情：catalog 全行 + volume/disk + 盘上副本 + 快照库 meta 键值。"""
    state = get_state(request)
    row = _require_snapshot(state, snapshot_id)
    detail = _rowget(row)
    vol = state.catalog.execute(
        "SELECT v.*, d.physical_model, d.physical_serial, d.capacity_bytes AS disk_capacity_bytes"
        " FROM volumes v LEFT JOIN disks d ON d.disk_id = v.disk_id WHERE v.volume_id=?",
        (row["volume_id"],),
    ).fetchone()
    detail["volume"] = _rowget(vol) if vol else None
    copies = state.catalog.execute(
        "SELECT * FROM on_disk_copies WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    detail["on_disk_copy"] = _rowget(copies) if copies else None
    conn = _snap_db(request, snapshot_id)
    detail["meta"] = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    return detail
