"""跨快照全局搜索（P4.5-fix-60）：GET /api/search —— 顶栏快速搜索 / Ctrl+K。

从 catalog 取 sealed 快照（collected_at 倒序，最多 20 个）逐库查询后合并：

- mode=prefix（默认）：name 前缀匹配（LIKE ? ESCAPE，可走 idx_entries_name NOCASE）；
- mode=fulltext：该库有 entries_fts 则 FTS5 MATCH（短语查询经
  ``seal.fts_match_query`` 安全转义），否则回退前缀；响应 scanned.fulltext_used
  表示是否至少有一库真正走了全文索引。

合并排序键 (collected_at DESC, snapshot_id ASC, path COLLATE NOCASE)；
keyset 游标为三元组自定义 base64url（pagination.encode_cursor 只支持单键，
风格与其保持一致）。每库最多取 limit+1 条做归并，20 库合计毫秒级。
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ..catalog import snapshot_path
from ..db import open_snapshot
from ..seal import fts_available, fts_match_query
from .state import get_state

router = APIRouter(prefix="/api/search", tags=["search"])

_LIMIT_DEFAULT = 50
_LIMIT_MAX = 200
_MAX_SNAPSHOTS = 20
_MODES = ("prefix", "fulltext")
_TYPES = ("file", "dir")

_SELECT_COLS = "entry_id, path, name, type, size_bytes, mtime_ns"


# ---------------------------------------------------------------- 游标


def _encode_cursor(collected_at: str, snapshot_id: str, path: str) -> str:
    payload = json.dumps({"ca": collected_at, "sid": snapshot_id, "p": path},
                         ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> "tuple[str, str, str]":
    """解析游标 → (collected_at, snapshot_id, path)；非法抛 ValueError。"""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        obj = json.loads(raw)
        ca, sid, p = obj["ca"], obj["sid"], obj["p"]
        if not (isinstance(ca, str) and isinstance(sid, str) and isinstance(p, str)):
            raise ValueError("cursor 字段必须为字符串")
        return ca, sid, p
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError,
            TypeError, ValueError) as e:
        raise ValueError(f"非法 cursor：{e}") from e


# ---------------------------------------------------------------- 单库查询


def _snap_query_rows(conn: sqlite3.Connection, q: str, mode: str,
                     entry_type: "str | None", path_after: "str | None",
                     limit: int, fulltext_used: list) -> "list[sqlite3.Row]":
    """单库取一页（≤ limit 行，调用方传 limit+1 判断该库是否还有更多）。

    path_after：keyset 推进时本库的 path 下界（COLLATE NOCASE 严格大于）。
    """
    where: "list[str]" = []
    params: "list[Any]" = []
    use_fts = mode == "fulltext" and fts_available(conn)
    if use_fts:
        fulltext_used.append(True)
        where.append("f.entries_fts MATCH ?")
        params.append(fts_match_query(q))
        src = "entries_fts f JOIN entries e ON e.entry_id = f.rowid"
        col = lambda c: f"e.{c}"  # noqa: E731
    else:
        # prefix（含 fulltext 无 FTS 的回退）：name 前缀，走 idx_entries_name
        escaped = q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        where.append(r"name LIKE ? ESCAPE '\'")
        params.append(escaped + "%")
        src = "entries e"
        col = lambda c: f"e.{c}"  # noqa: E731
    if entry_type:
        where.append(f"{col('type')} = ?")
        params.append(entry_type)
    if path_after is not None:
        where.append(f"{col('path')} > ? COLLATE NOCASE")
        params.append(path_after)
    try:
        return conn.execute(
            f"SELECT {', '.join(col(c) for c in _SELECT_COLS.split(', '))}"
            f" FROM {src} WHERE {' AND '.join(where)}"
            f" ORDER BY {col('path')} COLLATE NOCASE, {col('entry_id')} LIMIT ?",
            [*params, limit],
        ).fetchall()
    except sqlite3.OperationalError as e:
        if "fts5" in str(e).lower():
            raise HTTPException(status_code=400,
                                detail=f"搜索语法无效：{q!r}") from None
        raise


# ---------------------------------------------------------------- 端点


@router.get("")
def global_search(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1),
    cursor: "str | None" = Query(default=None),
    mode: str = Query(default="prefix"),
    volume_id: "str | None" = Query(default=None),
    snapshot_id: "str | None" = Query(default=None),
    type: "str | None" = Query(default=None),
) -> dict:
    """跨快照全局搜索：prefix（默认）或 fulltext，按快照新旧 + 路径排序。"""
    if len(q.strip()) < 2:
        raise HTTPException(status_code=400, detail="搜索词至少 2 个字符")
    if mode not in _MODES:
        raise HTTPException(status_code=400,
                            detail=f"非法 mode：{mode!r}（允许 {'/'.join(_MODES)}）")
    if type is not None and type not in _TYPES:
        raise HTTPException(status_code=400,
                            detail=f"非法 type：{type!r}（允许 {'/'.join(_TYPES)}）")
    if limit > _LIMIT_MAX:
        raise HTTPException(status_code=400, detail=f"limit 上限 {_LIMIT_MAX}")

    key: "tuple[str, str, str] | None" = None
    if cursor is not None:
        try:
            key = _decode_cursor(cursor)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None

    state = get_state(request)
    cat_sql = ("SELECT snapshot_id, volume_id, collected_at FROM snapshots"
               " WHERE status = 'sealed'")
    cat_params: "list[Any]" = []
    if volume_id is not None:
        cat_sql += " AND volume_id = ?"
        cat_params.append(volume_id)
    if snapshot_id is not None:
        cat_sql += " AND snapshot_id = ?"
        cat_params.append(snapshot_id)
    cat_sql += " ORDER BY collected_at DESC LIMIT ?"
    cat_params.append(_MAX_SNAPSHOTS)
    snaps = state.catalog.execute(cat_sql, cat_params).fetchall()

    merged: "list[dict]" = []
    fulltext_used: list = []
    scanned = 0
    for snap in snaps:
        ca, sid = snap["collected_at"] or "", snap["snapshot_id"]
        # keyset 窗口：游标所在快照仍要查（带 path 下界）；其余快照须整体
        # 在游标之后（排序为 (ca DESC, sid ASC)：ca 更小，或 ca 相同且 sid 更大）
        if key is not None:
            same = ca == key[0] and sid == key[1]
            if not same and not (ca < key[0] or (ca == key[0] and sid > key[1])):
                continue
        path_after = key[2] if (key is not None and ca == key[0]
                                and sid == key[1]) else None
        db = snapshot_path(state.data_root, sid)
        if not db.is_file():
            continue
        try:
            conn = open_snapshot(db)
        except sqlite3.Error:
            continue
        try:
            rows = _snap_query_rows(conn, q, mode, type, path_after, limit + 1,
                                    fulltext_used)
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        scanned += 1
        for r in rows:
            merged.append({
                "snapshot_id": sid,
                "volume_id": snap["volume_id"],
                "collected_at": snap["collected_at"],
                "entry_id": r["entry_id"],
                "path": r["path"],
                "name": r["name"],
                "type": r["type"],
                "size_bytes": r["size_bytes"],
                "mtime_ns": r["mtime_ns"],
            })

    # (ca DESC, sid ASC, path NOCASE ASC)：先按 (sid, path) 升序，再稳定按 ca 降序
    merged.sort(key=lambda it: (it["snapshot_id"], it["path"].casefold()))
    merged.sort(key=lambda it: it["collected_at"] or "", reverse=True)

    has_more = len(merged) > limit
    page = merged[:limit]
    last = page[-1] if has_more and page else None
    return {
        "q": q,
        "mode": mode,
        "items": page,
        "has_more": has_more,
        "next_cursor": (_encode_cursor(last["collected_at"] or "",
                                       last["snapshot_id"], last["path"])
                        if last else None),
        "scanned": {
            "snapshots": scanned,
            "fulltext_used": bool(fulltext_used),
        },
    }
