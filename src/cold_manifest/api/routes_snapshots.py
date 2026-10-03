"""snapshots 相关只读端点（§6.2）：列表/详情/stats/entries/tree/du/search/skipped/report。"""

import io
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from ..catalog import find_snapshot, snapshot_path
from ..exporter import export_v1_csv_zip, iter_snapshot_csv
from ..seal import build_fts, fts_available, fts_match_query
from ..stats_cache import build_stats_cache, compute_stats, load_precomputed_stats
from ..hash import ALLOWED_ALGOS, ALLOWED_POLICIES, run_hash_task
from ..seal import fts_available, fts_match_query
from ..stats_cache import compute_stats, load_precomputed_stats
from ..tasks import TaskRunner, register_task_fn
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


def display_label(vnick: "str | None", dnick: "str | None", volume_id: str) -> str:
    """快照展示标签：与 catalog.snapshot_label 同口径，供 JOIN 结果就地计算。

    卷昵（盘昵）· volume_id → 盘昵 · volume_id → ""（列表用，避免 N+1 查询）。
    """
    label = ""
    if vnick:
        label = vnick + (f"（{dnick}）" if dnick else "")
    elif dnick:
        label = dnick
    return f"{label} · {volume_id}" if label else ""


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
           s.zero_byte_count, s.max_depth, s.skipped_count, s.host_path, s.pinned,
           v.disk_id, v.filesystem, v.label, v.capacity_bytes AS volume_capacity_bytes,
           v.nickname AS volume_nickname,
           d.physical_model, d.physical_serial, d.capacity_bytes AS disk_capacity_bytes,
           d.nickname AS disk_nickname
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
    from ..identity_audit import disk_identity_status

    status_cache: "dict[str, tuple[str, str]]" = {}
    for item in items:
        item["pinned"] = bool(item.get("pinned"))
        item["label"] = display_label(item.get("volume_nickname"),
                                      item.get("disk_nickname"), item["volume_id"])
        did = item.get("disk_id")
        if did not in status_cache:
            status_cache[did] = disk_identity_status(
                state.catalog, state.data_root, did) if did else ("unknown", "无关联磁盘")
        item["identity_status"], item["identity_status_reason"] = status_cache[did]
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------- 详情
# 注意：/{snapshot_id:path} 会吞掉带斜杠的子路径，必须最后注册（见文件底部）


# ---------------------------------------------------------------- stats


@router.get("/{snapshot_id:path}/stats")
def snapshot_stats(snapshot_id: str, request: Request) -> dict:
    """扩展名分布（按大小/数量）、大小直方图、top 50 大文件、零字节计数、深度直方图。

    优先直读封库期预计算的 stats_precomputed（P2-A，10M 库实时聚合 6–9.6s →
    毫秒级）；旧快照无该表（或 key 残缺）→ 回退实时聚合，接口形状不变。
    两种路径共用 stats_cache.compute_stats，结果严格一致；响应多带
    precomputed=true|false 标注来源。
    """
    conn = _snap_db(request, snapshot_id)
    pre = load_precomputed_stats(conn)
    if pre is not None:
        return {"snapshot_id": snapshot_id, **pre, "precomputed": True}
    return {"snapshot_id": snapshot_id, **compute_stats(conn), "precomputed": False}


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
    mode: Literal["prefix", "fulltext"] = Query(default="prefix"),
    type: "EntryType | None" = Query(default=None),
    ext: "str | None" = Query(default=None),
    min_size: "int | None" = Query(default=None, ge=0),
    max_size: "int | None" = Query(default=None, ge=0),
    cursor: "str | None" = Query(default=None),
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
) -> dict:
    """搜索：mode=prefix 走 idx_entries_name（NOCASE）；mode=fulltext 走 entries_fts（FTS5）。

    fulltext 按 path（COLLATE NOCASE）keyset 分页（与契约一致，不用 rank 排序）；
    查询词安全转义为 FTS 短语（内部引号翻倍外包双引号），MATCH 语法错误 → 400。
    快照未建 FTS（旧快照可用 `cldm build-fts` 补建）时回退 substring
    （LIKE %q%），响应带 fulltext_available=false 供前端提示/禁用。
    """
    conn = _snap_db(request, snapshot_id)
    has_fts = fts_available(conn)

    if mode == "fulltext" and has_fts:
        return _search_fulltext(conn, snapshot_id, q, type, ext, min_size, max_size,
                                cursor, limit, fallback=False)

    if mode == "fulltext":
        # 无 FTS 表：回退 substring（LIKE 包含匹配）
        return _search_prefix(conn, snapshot_id, q, mode="substring", substring=True,
                              type=type, ext=ext, min_size=min_size, max_size=max_size,
                              cursor=cursor, limit=limit, has_fts=False)

    return _search_prefix(conn, snapshot_id, q, mode="prefix", substring=False,
                          type=type, ext=ext, min_size=min_size, max_size=max_size,
                          cursor=cursor, limit=limit, has_fts=has_fts)


def _search_fulltext(conn: Any, snapshot_id: str, q: str,
                     type: "EntryType | None", ext: "str | None",
                     min_size: "int | None", max_size: "int | None",
                     cursor: "str | None", limit: int, fallback: bool) -> dict:
    op = ">"
    where = ["f.entries_fts MATCH ?"]
    params: "list[Any]" = [fts_match_query(q)]
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
    if cursor is not None:
        try:
            key_val, last_id = decode_cursor(cursor)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        where.append(_keyset_clause("e.path", True, op, id_col="e.entry_id"))
        params += [key_val, key_val, last_id]

    try:
        rows = conn.execute(
            f"""
            SELECT e.entry_id, e.name, e.path, e.type, e.size_bytes, e.ext
            FROM entries_fts f JOIN entries e ON e.entry_id = f.rowid
            WHERE {' AND '.join(where)}
            ORDER BY e.path COLLATE NOCASE, e.entry_id
            LIMIT ?
            """,
            [*params, limit + 1],
        ).fetchall()
    except sqlite3.OperationalError as e:
        if "fts5" in str(e).lower():
            raise HTTPException(status_code=400, detail=f"搜索语法无效：{q!r}") from None
        raise
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_cursor(rows[-1]["path"], rows[-1]["entry_id"]) if has_more and rows else None
    )
    return {
        "snapshot_id": snapshot_id,
        "mode": "fulltext",
        "fulltext_available": True,
        "items": [
            {"entry_id": r["entry_id"], "name": r["name"], "path": r["path"],
             "type": r["type"], "size_bytes": r["size_bytes"], "ext": r["ext"]}
            for r in rows
        ],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def _search_prefix(conn: Any, snapshot_id: str, q: str, mode: str, substring: bool,
                   type: "EntryType | None", ext: "str | None",
                   min_size: "int | None", max_size: "int | None",
                   cursor: "str | None", limit: int, has_fts: bool) -> dict:
    """前缀搜索（idx_entries_name）或 substring 回退（LIKE %q%），按 name keyset 分页。"""
    op = ">"
    where = [r"name LIKE ? ESCAPE '\'"]
    escaped = q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    prefix = escaped + "%" if not substring else "%" + escaped + "%"
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
        "fulltext_available": has_fts,
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


# ---------------------------------------------------------------- 删除


@router.delete("/{snapshot_id:path}")
def delete_snapshot_ep(
    snapshot_id: str,
    request: Request,
    on_disk: Literal["keep", "delete"] = Query(default="keep"),
    force: bool = Query(default=False),
) -> dict:
    """删除快照（P4-①）：主机快照目录 +（可选）盘上副本 + catalog 注册行。

    404=快照不存在；400=snapshot_id/on_disk 非法；409=被 diff 引用（?force=true
    级联）或有活跃任务；持 DataRootLock 与采集/哈希互斥，占用 → 409。
    """
    from ..catalog import (
        SnapshotDeleteBlocked,
        SnapshotDeleteError,
        delete_snapshot as _delete_snapshot,
    )
    from ..lockfile import DataRootLock, LockBusy

    state = get_state(request)
    lock = DataRootLock(state.data_root)
    try:
        lock.acquire()
    except LockBusy as e:
        raise HTTPException(status_code=409, detail=f"数据根被占用：{e}") from None

    def _fail_busy(exc: PermissionError) -> HTTPException:
        state.catalog.rollback()
        where = getattr(exc, "filename", None) or snapshot_id
        return HTTPException(
            status_code=409,
            detail=f"{where}：文件被占用或只读，请关闭正在进行的导出/查看后重试")

    try:
        # Windows：池内缓存的快照库只读句柄会锁住文件，删除前先释放
        state.evict_snapshot(snapshot_id)
        try:
            return _delete_snapshot(state.catalog, state.data_root, snapshot_id,
                                    on_disk=on_disk, force=force)
        except PermissionError:
            # 可能仍有在途流式导出持有连接：关掉后重试一次；
            # 仍失败则明确报错，绝不谎报成功（Linux unlink 语义不会走到这里）
            state.evict_snapshot(snapshot_id)
            try:
                return _delete_snapshot(state.catalog, state.data_root, snapshot_id,
                                        on_disk=on_disk, force=force)
            except PermissionError as e:
                raise _fail_busy(e) from None
    except LookupError as e:
        state.catalog.rollback()
        raise HTTPException(status_code=404, detail=str(e)) from None
    except SnapshotDeleteError as e:
        state.catalog.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from None
    except SnapshotDeleteBlocked as e:
        state.catalog.rollback()
        detail: "dict | str" = str(e)
        if e.diffs or e.tasks:
            detail = {"message": str(e), "diffs": e.diffs, "tasks": e.tasks}
        raise HTTPException(status_code=409, detail=detail) from None
    finally:
        lock.release()


# ---------------------------------------------------------------- 报告（P4-⑦）
# 注意：必须位于文件底部 catch-all 详情端点之前，否则 /report 会被吞


@router.get("/{snapshot_id:path}/report")
def snapshot_report(
    snapshot_id: str,
    request: Request,
    format: str = Query(default="html"),
    max_rows: int = Query(default=1000, ge=1, le=10000),
    sections: "str | None" = Query(default=None),
):
    """快照自包含 HTML 报告：format=html（其他值 → 400）。

    头部元信息/SMART 摘要取自 catalog；聚合段优先 stats_precomputed 直读
    （旧库回退实时聚合，响应内容一致）。
    sections：逗号分隔的段落名（overview,extensions,sizes,depth,topdirs,skipped），
    默认全含；未知值 → 400。
    """
    from ..report import REPORT_SECTIONS, generate_snapshot_report
    from fastapi.responses import HTMLResponse

    if format != "html":
        raise HTTPException(status_code=400,
                            detail=f"非法 format：{format!r}（当前仅支持 html）")
    section_list: "list[str] | None" = None
    if sections is not None:
        section_list = [s.strip() for s in sections.split(",") if s.strip()]
        if not section_list:
            raise HTTPException(status_code=400, detail="sections 不能为空")
        unknown = [s for s in section_list if s not in REPORT_SECTIONS]
        if unknown:
            raise HTTPException(status_code=400,
                                detail=f"未知 sections：{', '.join(unknown)}"
                                       f"（允许 {'/'.join(REPORT_SECTIONS)}）")
    state = get_state(request)
    row = _require_snapshot(state, snapshot_id)
    conn = _snap_db(request, snapshot_id)
    vol = state.catalog.execute(
        "SELECT v.*, d.physical_model FROM volumes v"
        " LEFT JOIN disks d ON d.disk_id = v.disk_id WHERE v.volume_id=?",
        (row["volume_id"],),
    ).fetchone()
    smart_row = None
    if vol is not None:
        smart_row = state.catalog.execute(
            "SELECT * FROM disk_smart WHERE disk_id=?"
            " ORDER BY collected_at DESC LIMIT 1",
            (vol["disk_id"],),
        ).fetchone()
    buf = io.StringIO()
    generate_snapshot_report(conn, buf, snapshot_id=snapshot_id,
                             snapshot_row=row, volume_row=vol,
                             smart_row=smart_row, max_rows_per_section=max_rows,
                             sections=section_list)
    return HTMLResponse(content=buf.getvalue(), media_type="text/html")


# ---------------------------------------------------------------- 校验副本（verify-copy）


class VerifyCopyBody(BaseModel):
    scope: str = "sample"           # sample（默认抽检）| full（全部重算）
    sample_size: int = 200
    seed: "int | None" = None


_VERIFY_SAMPLE_MAX = 5000


@router.post("/{snapshot_id:path}/verify-copy")
def verify_copy_ep(snapshot_id: str, body: VerifyCopyBody, request: Request) -> dict:
    """同步校验盘上副本与（可选）源文件完整性，返回与 CLI verify-copy 同源的报告。

    同步 def：FastAPI 自动放线程池执行，不阻塞事件循环；校验只读，不持写锁。
    404=快照未注册/不存在；400=参数非法或校验无法进行（未封库/扫描根不可定位）。
    """
    from ..verify import VerifyError, verify_snapshot_copy

    if body.scope not in ("sample", "full"):
        raise HTTPException(status_code=400,
                            detail=f"非法 scope：{body.scope!r}（允许 sample/full）")
    if not isinstance(body.sample_size, int) or not (1 <= body.sample_size <= _VERIFY_SAMPLE_MAX):
        raise HTTPException(status_code=400,
                            detail=f"sample_size 须为 1–{_VERIFY_SAMPLE_MAX} 的整数："
                                   f"{body.sample_size!r}")

    state = get_state(request)
    _require_snapshot(state, snapshot_id)
    try:
        return verify_snapshot_copy(
            state.data_root, snapshot_id,
            sample=0 if body.scope == "full" else body.sample_size,
            seed=body.seed, full=body.scope == "full")
    except VerifyError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


# ---------------------------------------------------------------- 构建 FTS / stats 预计算（任务化）

_KIND_BUILD_FTS = "build_fts"
_KIND_BUILD_STATS = "build_stats"


def _evict_snapshot_pools(snapshot_id: str) -> None:
    """就地写快照库（build_fts/build_stats/hash）前后，逐出 API 进程内该快照的池连接。

    池连接虽已是 mode=ro 普通只读（不再 immutable），但旧连接仍持文件句柄与
    页缓存；写任务执行前逐出避免与在途读交叉，执行后逐出让后续读拿到新连接、
    并在 Windows 上释放旧句柄。任务在 API 进程内执行时同步逐出（uvicorn 的
    模块级 app）；经 CLI 等其他途径执行时静默跳过。
    """
    try:
        from ..server import app as _app
        state = getattr(_app.state, "cldm", None)
        if state is not None:
            state.evict_snapshot(snapshot_id)
    except Exception:
        pass


def _run_seal_upgrade(payload: dict, progress_cb: Any, cancel_event: Any = None,
                      *, kind: str) -> dict:
    """build_fts / build_stats 共用执行体：持 data_root 写锁，可写打开快照库就地重建。

    参照 run_hash_task：锁被占 → RuntimeError（任务落 error）；快照库缺失 → LookupError。
    """
    from ..lockfile import DataRootLock, LockBusy

    data_root = Path(payload["data_root"])
    lock = DataRootLock(data_root)
    try:
        lock.acquire()
    except LockBusy as e:
        raise RuntimeError(f"数据根被占用：{e}") from None
    try:
        db_path = snapshot_path(data_root, payload["snapshot_id"])
        if not db_path.is_file():
            raise LookupError(f"快照库文件缺失：{payload['snapshot_id']}")
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            t0 = time.monotonic()
            if kind == _KIND_BUILD_FTS:
                count, tok = build_fts(conn)
                conn.commit()
                _evict_snapshot_pools(payload["snapshot_id"])
                return {"built": True, "tokenizer": tok, "count": count,
                        "seconds": round(time.monotonic() - t0, 3)}
            keys = build_stats_cache(conn)
            conn.commit()
            _evict_snapshot_pools(payload["snapshot_id"])
            return {"keys": keys, "seconds": round(time.monotonic() - t0, 3)}
        finally:
            conn.close()
    finally:
        lock.release()


def _run_build_fts(payload: dict, progress_cb: Any, cancel_event: Any = None) -> dict:
    return _run_seal_upgrade(payload, progress_cb, cancel_event, kind=_KIND_BUILD_FTS)


def _run_build_stats(payload: dict, progress_cb: Any, cancel_event: Any = None) -> dict:
    return _run_seal_upgrade(payload, progress_cb, cancel_event, kind=_KIND_BUILD_STATS)


register_task_fn(_KIND_BUILD_FTS, _run_build_fts)
register_task_fn(_KIND_BUILD_STATS, _run_build_stats)


def _require_sealed(request: Request, snapshot_id: str) -> None:
    """快照库 meta.status=sealed 才允许就地重建索引/预计算，否则 400。"""
    conn = _snap_db(request, snapshot_id)
    row = conn.execute("SELECT value FROM meta WHERE key='status'").fetchone()
    if row is None or row[0] != "sealed":
        raise HTTPException(status_code=400,
                            detail=f"快照未封库，不能就地重建（meta.status={row[0] if row else None!r}）")


@router.post("/{snapshot_id:path}/build-fts", status_code=201)
def submit_build_fts(snapshot_id: str, request: Request) -> dict:
    """提交 FTS5 全文索引构建任务（幂等重建）：404/400 未封库/409 同快照重复提交。"""
    state = get_state(request)
    _require_snapshot(state, snapshot_id)
    _require_sealed(request, snapshot_id)
    runner: TaskRunner = request.app.state.task_runner
    payload = {"snapshot_id": snapshot_id, "data_root": str(state.data_root)}
    task_id = runner.submit_dedup(_KIND_BUILD_FTS, payload, field="snapshot_id")
    if task_id is None:
        raise HTTPException(status_code=409, detail="同快照已有 pending/running 的 build_fts 任务")
    return {"task_id": task_id, "status": "pending"}


@router.post("/{snapshot_id:path}/build-stats", status_code=201)
def submit_build_stats(snapshot_id: str, request: Request) -> dict:
    """提交 stats_precomputed 预计算任务（幂等重建）：404/400 未封库/409 同快照重复提交。"""
    state = get_state(request)
    _require_snapshot(state, snapshot_id)
    _require_sealed(request, snapshot_id)
    runner: TaskRunner = request.app.state.task_runner
    payload = {"snapshot_id": snapshot_id, "data_root": str(state.data_root)}
    task_id = runner.submit_dedup(_KIND_BUILD_STATS, payload, field="snapshot_id")
    if task_id is None:
        raise HTTPException(status_code=409, detail="同快照已有 pending/running 的 build_stats 任务")
    return {"task_id": task_id, "status": "pending"}


# ---------------------------------------------------------------- PATCH 详情（pinned / notes）


class SnapshotPatchBody(BaseModel):
    pinned: "bool | None" = None
    notes: "str | None" = None


_NOTES_MAX = 2000


@router.patch("/{snapshot_id:path}")
def patch_snapshot(snapshot_id: str, body: SnapshotPatchBody, request: Request) -> dict:
    """更新 catalog.snapshots 的 pinned / notes（两者都缺 → 400；notes ≤2000 字符）。

    catalog 连接与 TaskRunner 共享：写事务沿用 tasks.submit 的 _lock 串行化约定，
    短事务（单 UPDATE + commit）持锁执行，避免与工作线程写事务交错。
    """
    from ..catalog import validate_volume_id

    if body.pinned is None and body.notes is None:
        raise HTTPException(status_code=400, detail="pinned 与 notes 至少提供一个")
    if body.notes is not None and len(body.notes) > _NOTES_MAX:
        raise HTTPException(status_code=400,
                            detail=f"notes 超长（最多 {_NOTES_MAX} 字符）：{len(body.notes)}")

    try:
        validate_volume_id(snapshot_id.split("/", 1)[0])
    except Exception as e:  # LegacyImportError 等：非法 sid 一律 400
        raise HTTPException(status_code=400, detail=str(e)) from None

    state = get_state(request)
    _require_snapshot(state, snapshot_id)
    sets: "list[str]" = []
    params: "list[Any]" = []
    if body.pinned is not None:
        sets.append("pinned = ?")
        params.append(1 if body.pinned else 0)
    if body.notes is not None:
        sets.append("notes = ?")
        params.append(body.notes)
    params.append(snapshot_id)
    runner: TaskRunner = request.app.state.task_runner
    with runner._lock:  # 与 TaskRunner 共享 catalog 连接：写事务全程持锁
        state.catalog.execute(
            f"UPDATE snapshots SET {', '.join(sets)} WHERE snapshot_id = ?", params)
        state.catalog.commit()
    return snapshot_detail(snapshot_id, request)


# ---------------------------------------------------------------- 报告 sections 过滤已在 report 端点支持

# ---------------------------------------------------------------- 详情（最后注册：:path 转换器会吞子路径）

@router.get("/{snapshot_id:path}")
def snapshot_detail(snapshot_id: str, request: Request) -> dict:
    """详情：catalog 全行 + volume/disk + 盘上副本 + 快照库 meta 键值。"""
    state = get_state(request)
    row = _require_snapshot(state, snapshot_id)
    detail = _rowget(row)
    vol = state.catalog.execute(
        "SELECT v.*, d.physical_model, d.physical_serial,"
        " d.capacity_bytes AS disk_capacity_bytes, d.nickname AS disk_nickname"
        " FROM volumes v LEFT JOIN disks d ON d.disk_id = v.disk_id WHERE v.volume_id=?",
        (row["volume_id"],),
    ).fetchone()
    detail["volume"] = _rowget(vol) if vol else None
    from ..identity_audit import disk_identity_status

    if vol is not None and vol["disk_id"]:
        detail["identity_status"], detail["identity_status_reason"] = \
            disk_identity_status(state.catalog, state.data_root, vol["disk_id"])
    else:
        detail["identity_status"] = "unknown"
        detail["identity_status_reason"] = "无关联磁盘"
    detail["volume_nickname"] = vol["nickname"] if vol else None
    detail["disk_nickname"] = vol["disk_nickname"] if vol else None
    detail["label"] = (display_label(detail["volume_nickname"],
                                     detail["disk_nickname"], row["volume_id"])
                       if vol else "")
    copies = state.catalog.execute(
        "SELECT * FROM on_disk_copies WHERE snapshot_id=?", (snapshot_id,)
    ).fetchone()
    detail["on_disk_copy"] = _rowget(copies) if copies else None
    conn = _snap_db(request, snapshot_id)
    detail["meta"] = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    return detail


# ---------------------------------------------------------------- 按需哈希（§4.5）

_KIND_HASH = "hash"


class HashBody(BaseModel):
    algo: str = "sha256"
    policy: str = "full"
    root: str | None = None
    scope: str = "incremental"      # incremental（默认）| candidates（只算大小候选集）
    group: str | None = None        # 指定 hash_hex：只对该组做全量精验（与 candidates 互斥）


def _run_hash(payload: dict, progress_cb: Any, cancel_event: Any = None) -> dict:
    """工作线程执行体（经 tasks._FN_REGISTRY 调用）：持 data_root 锁同步哈希。

    哈希会就地写快照库（回填 entries.hash_*），与 build-fts/build-stats 一样
    必须写前/写后逐出该快照的池连接（_evict_snapshot_pools）：写前避免带旧
    连接写、写后让后续读拿到新连接。任务跑在 TaskRunner 工作线程里拿不到
    request/app state，复用 build-* 的"模块级 app 查找、CLI 下静默跳过"路径。
    """
    _evict_snapshot_pools(payload["snapshot_id"])       # 写前逐出
    try:
        return run_hash_task(payload, progress_cb, cancel_event)
    finally:
        _evict_snapshot_pools(payload["snapshot_id"])   # 写后逐出


register_task_fn(_KIND_HASH, _run_hash)


@router.post("/{snapshot_id:path}/hash", status_code=201)
def submit_hash(snapshot_id: str, body: HashBody, request: Request) -> dict:
    """提交按需哈希任务：body {algo?: "sha256", policy?: "full"|"sampled", root?: 源目录覆盖}。

    404=快照不存在；400=algo/policy 非法；409=同快照已有 pending/running 哈希任务。
    """
    state = get_state(request)
    _require_snapshot(state, snapshot_id)
    if body.algo not in ALLOWED_ALGOS:
        raise HTTPException(status_code=400,
                            detail=f"不支持的哈希算法：{body.algo!r}（允许 {'/'.join(ALLOWED_ALGOS)}）")
    if body.policy not in ALLOWED_POLICIES:
        raise HTTPException(status_code=400,
                            detail=f"非法哈希策略：{body.policy!r}（允许 {'/'.join(ALLOWED_POLICIES)}）")
    if body.scope not in ("incremental", "candidates"):
        raise HTTPException(status_code=400,
                            detail=f"非法 scope：{body.scope!r}（允许 incremental/candidates）")
    if body.group is not None and body.scope == "candidates":
        raise HTTPException(status_code=400,
                            detail="group 与 candidates 互斥：group 是对指定哈希组做"
                                   "全量精验，candidates 是批量补算大小候选集")

    runner: TaskRunner = request.app.state.task_runner
    payload = {"snapshot_id": snapshot_id, "algo": body.algo, "policy": body.policy,
               "data_root": str(state.data_root), "root": body.root,
               "scope": body.scope, "group": body.group}
    task_id = runner.submit_dedup(_KIND_HASH, payload, field="snapshot_id")
    if task_id is None:
        raise HTTPException(status_code=409, detail="同快照已有 pending/running 的哈希任务")
    return {"task_id": task_id, "status": "pending"}
