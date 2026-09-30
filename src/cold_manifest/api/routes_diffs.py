"""diff 端点（§6.2）：物化 / 状态 / summary / entries / 导出。

POST 物化为同步执行（FastAPI sync 端点自动跑线程池），幂等复用既有结果库；
查询一律走只读打开的 diff 结果库（<data_root>/_diffs/<diff_id>.db）。
"""

import itertools
import io
import json
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..diff_engine import (DiffError, diff_db_path, iter_diff_csv, materialize_diff,
                           _read_evidence)
from ..report import generate_diff_report
from .pagination import decode_cursor, encode_cursor
from .state import AppState, get_state

router = APIRouter(prefix="/api/diffs", tags=["diffs"])

_LIMIT_DEFAULT = 200
_LIMIT_MAX = 500

# 与 diff_engine._COUNTED 一致的分类白名单（content/moved 无哈希恒 NULL，也允许查询）
_CATEGORIES = {"added", "removed", "type_changed", "size_changed",
               "mtime_changed", "content_changed", "moved_or_renamed", "identical"}

SortKey = Literal["path", "size_delta"]


def _open_diff_ro(state: AppState, diff_id: str) -> sqlite3.Connection:
    """按 diff_id 只读打开结果库；不存在或状态非 done → 404。"""
    db = diff_db_path(state.data_root, diff_id)
    if not db.is_file():
        raise HTTPException(status_code=404, detail=f"diff 不存在：{diff_id}")
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT value FROM diff_meta WHERE key='status'").fetchone()
    if not row or row[0] != "done":
        conn.close()
        raise HTTPException(status_code=404, detail=f"diff 未完成或状态异常：{diff_id}")
    return conn


def _hash_cols(conn: sqlite3.Connection) -> "tuple[str, str]":
    """旧 diff 库可能无 a_hash/b_hash/b_path 列：存在才查询（兼容读取）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(diff_entries)")}
    if "a_hash" in cols:
        return "a_hash, b_hash, b_path", ""
    return "NULL AS a_hash, NULL AS b_hash, NULL AS b_path", ""


def _require_diff_run(state: AppState, diff_id: str) -> Any:
    row = state.catalog.execute(
        "SELECT * FROM diff_runs WHERE diff_id=?", (diff_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"diff 不存在：{diff_id}")
    return row


# ---------------------------------------------------------------- 物化


class DiffCreateBody(BaseModel):
    a: str
    b: str
    options: "dict | None" = None
    hash: "str | None" = None     # 便捷字段：等价 options={"hash": ...}
    case_insensitive: "bool | None" = None    # 按 path_norm（casefold）配对
    ignore_mtime: "bool | None" = None        # mtime 差异不算变更
    ignore_size: "bool | None" = None         # 大小差异不算变更
    show_identical: "bool | None" = None      # identical 也落库（大库膨胀，慎用）


@router.post("")
def create_diff(body: DiffCreateBody, request: Request) -> dict:
    """同步物化 diff(a, b, options)；同参幂等复用既有结果库。"""
    if not body.a or not body.b:
        raise HTTPException(status_code=400, detail="a 与 b 不能为空")
    options = dict(body.options or {})
    if body.hash is not None:
        options["hash"] = body.hash
    for flag in ("case_insensitive", "ignore_mtime", "ignore_size", "show_identical"):
        val = getattr(body, flag)
        if val is not None:
            options[flag] = val
    state = get_state(request)
    try:
        result = materialize_diff(state.data_root, body.a, body.b, options=options)
    except DiffError as e:
        msg = str(e)
        code = 404 if "不存在" in msg else 400
        raise HTTPException(status_code=code, detail=msg) from None
    return {
        "diff_id": result.diff_id,
        "status": "done",
        "counts": result.counts,
        "evidence_level": result.evidence_level,
        "evidence": result.evidence,
        "reused": result.reused,
        "elapsed_s": round(result.elapsed_s, 3),
    }


# ---------------------------------------------------------------- 状态 / summary


@router.get("/{diff_id}")
def diff_detail(diff_id: str, request: Request) -> dict:
    """状态 / 选项 / 分类计数（catalog.diff_runs + 结果库 diff_meta）。"""
    state = get_state(request)
    run = _require_diff_run(state, diff_id)
    detail = dict(run)
    for key in ("options_json", "summary_json"):
        if detail.get(key):
            try:
                detail[key.removesuffix("_json")] = json.loads(detail.pop(key))
            except json.JSONDecodeError:
                detail.pop(key)
    ev_level, ev = _read_evidence(diff_db_path(state.data_root, diff_id))
    if ev is not None:
        detail["evidence_level"] = ev_level
        detail["evidence"] = ev
    return detail


@router.get("/{diff_id}/summary")
def diff_summary(diff_id: str, request: Request,
                 top: int = Query(default=50, ge=1, le=500)) -> dict:
    """分类计数 + 按父目录聚合的变更 top-N（数量、size 增减）+ 总 size 变化。"""
    conn = _open_diff_ro(get_state(request), diff_id)
    state = get_state(request)
    try:
        total = conn.execute(
            "SELECT COUNT(*) AS n,"
            " SUM(COALESCE(b_size, 0) - COALESCE(a_size, 0)) AS size_delta"
            " FROM diff_entries").fetchone()

        # 父目录 = 去掉末段文件名；SQLite 无内置 reverse()，
        # 用 rtrim(path, path 去掉所有 '/') 剥掉末段非 '/' 字符，再剥掉尾部 '/'
        by_dir = conn.execute(
            """
            SELECT CASE WHEN instr(path, '/') = 0 THEN '(root)'
                        ELSE rtrim(rtrim(path, replace(path, '/', '')), '/')
                   END AS parent_dir,
                   COUNT(*) AS n,
                   SUM(COALESCE(b_size, 0) - COALESCE(a_size, 0)) AS size_delta
            FROM diff_entries
            GROUP BY parent_dir
            ORDER BY n DESC, parent_dir
            LIMIT ?
            """,
            (top,),
        ).fetchall()
        payload = {
            "diff_id": diff_id,
            "total_changes": total["n"],
            "total_size_delta": total["size_delta"] or 0,
            "by_parent_dir": [
                {"parent_dir": r["parent_dir"], "count": r["n"],
                 "size_delta": r["size_delta"] or 0}
                for r in by_dir
            ],
        }
        ev_level, ev = _read_evidence(diff_db_path(state.data_root, diff_id))
        if ev is not None:
            payload["evidence_level"] = ev_level
            payload["evidence"] = ev
        return payload
    finally:
        conn.close()


# ---------------------------------------------------------------- entries


def _keyset_clause(key_expr: str, is_text: bool, op: str) -> str:
    coll = " COLLATE NOCASE" if is_text else ""
    return (f"({key_expr}{coll} {op} ?{coll}"
            f" OR ({key_expr}{coll} = ?{coll} AND id {op} ?))")


@router.get("/{diff_id}/entries")
def diff_entries(
    diff_id: str,
    request: Request,
    category: "str | None" = Query(default=None),
    path_prefix: "str | None" = Query(default=None, max_length=500),
    cursor: "str | None" = Query(default=None),
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
    sort: SortKey = Query(default="path"),
    order: Literal["asc", "desc"] = Query(default="asc"),
) -> dict:
    """变更明细 keyset 分页（cursor 含排序键 + 行 id）。"""
    if category is not None and category not in _CATEGORIES:
        raise HTTPException(status_code=400, detail=f"非法 category：{category}（允许 {_CATEGORIES}）")

    conn = _open_diff_ro(get_state(request), diff_id)
    try:
        size_delta = "COALESCE(b_size, 0) - COALESCE(a_size, 0)"
        key_expr, is_text = (("path", True) if sort == "path" else (size_delta, False))
        desc = order == "desc"
        op = "<" if desc else ">"

        where = ["1=1"]
        params: "list[Any]" = []
        if category is not None:
            where.append("change_type = ?")
            params.append(category)
        if path_prefix:
            where.append(r"path LIKE ? ESCAPE '\'")
            params.append(path_prefix.replace("\\", "\\\\").replace("%", r"\%")
                          .replace("_", r"\_") + "%")
        if cursor is not None:
            try:
                key_val, last_id = decode_cursor(cursor)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from None
            where.append(_keyset_clause(key_expr, is_text, op))
            params += [key_val, key_val, last_id]

        order_sql = "DESC" if desc else "ASC"
        # path 为文本排序键，须与 keyset 的 COLLATE NOCASE 一致，否则混合大小写翻页丢行
        coll = " COLLATE NOCASE" if is_text else ""
        hash_cols, _ = _hash_cols(conn)
        rows = conn.execute(
            f"""
            SELECT id, change_type AS category, path, depth,
                   a_type, b_type, {hash_cols},
                   a_size, b_size, a_mtime_ns, b_mtime_ns,
                   {size_delta} AS size_delta
            FROM diff_entries
            WHERE {' AND '.join(where)}
            ORDER BY {key_expr}{coll} {order_sql}, id {order_sql}
            LIMIT ?
            """,
            [*params, limit + 1],
        ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            key_val = last["path"] if sort == "path" else (last["size_delta"] or 0)
            next_cursor = encode_cursor(key_val, last["id"])
        items = []
        for r in rows:
            item = dict(r)
            # mtime_ns 超 JS Number.MAX_SAFE_INTEGER，与快照端点一致字符串化
            for k in ("a_mtime_ns", "b_mtime_ns"):
                if item.get(k) is not None:
                    item[k] = str(item[k])
            items.append(item)
        return {
            "diff_id": diff_id,
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- 报告


@router.get("/{diff_id}/report")
def diff_report(
    diff_id: str,
    request: Request,
    format: Literal["html"] = Query(default="html"),
) -> Response:
    """自包含 HTML 报告（§6.2 format=html）；未知 diff / 未物化 → 404。"""
    if format != "html":
        raise HTTPException(status_code=400,
                            detail=f"不支持的报告格式：{format!r}（当前仅支持 html）")
    state = get_state(request)
    run = _require_diff_run(state, diff_id)          # catalog 未登记 → 404
    conn = _open_diff_ro(state, diff_id)             # 结果库缺失/未完成 → 404
    try:
        meta = {r["key"]: r["value"] for r in
                conn.execute("SELECT key, value FROM diff_meta")}
        snap_meta = {}
        for side in ("a", "b"):
            sid = meta.get(side)
            if not sid:
                continue
            row = state.catalog.execute(
                "SELECT collected_at, total_bytes FROM snapshots WHERE snapshot_id=?",
                (sid,)).fetchone()
            if row:
                snap_meta[side] = {"collected_at": row["collected_at"],
                                   "total_bytes": row["total_bytes"]}
        buf = io.StringIO()
        try:
            generate_diff_report(diff_db_path(state.data_root, diff_id), buf,
                                 snapshot_meta=snap_meta or None)
        except FileNotFoundError:
            raise HTTPException(status_code=404,
                                detail=f"diff 结果库不存在（未物化或已删除）：{diff_id}") from None
    finally:
        conn.close()
    return Response(
        content=buf.getvalue(),
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="diff-{diff_id}.html"'},
    )


# ---------------------------------------------------------------- 导出


@router.get("/{diff_id}/export")
def diff_export(
    diff_id: str,
    request: Request,
    format: Literal["csv"] = Query(default="csv"),
    depth: "int | None" = Query(default=None, ge=0),
) -> StreamingResponse:
    """diff 结果导出 CSV（流式；与 CLI `cldm diff --output` 同一列结构）。"""
    state = get_state(request)
    _require_diff_run(state, diff_id)   # catalog 未登记 → 404
    _open_diff_ro(state, diff_id).close()   # 结果库缺失/未完成 → 404

    rows_iter = iter_diff_csv(diff_db_path(state.data_root, diff_id), depth=depth)

    def stream():
        import csv as _csv
        import io

        buf = io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["category", "path", "type", "size_a", "size_b", "mtime_a", "mtime_b"])
        yield buf.getvalue()
        while True:
            batch = list(itertools.islice(rows_iter, 8192))
            if not batch:
                return
            buf = io.StringIO()
            w = _csv.writer(buf)
            w.writerows(tuple(r) for r in batch)
            yield buf.getvalue()

    return StreamingResponse(
        stream(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="diff_{diff_id}.csv"'},
    )
