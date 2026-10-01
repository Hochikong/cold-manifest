"""volumes / disks 概览与 diffs 历史端点（§6.2，P0 范围内只读部分）。"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .pagination import decode_cursor, encode_cursor
from .state import get_state

router = APIRouter(prefix="/api", tags=["volumes"])


def _rowget(row: Any) -> dict:
    return dict(row)


@router.get("/volumes")
def list_volumes(request: Request) -> dict:
    """卷列表 + 所属磁盘概要 + 快照数。"""
    state = get_state(request)
    rows = state.catalog.execute(
        """
        SELECT v.volume_id, v.disk_id, v.partition_index, v.partition_uuid,
               v.volume_serial_hex, v.filesystem, v.label, v.capacity_bytes,
               v.nickname,
               d.physical_model, d.physical_serial, d.capacity_bytes AS disk_capacity_bytes,
               d.nickname AS disk_nickname,
               (SELECT COUNT(*) FROM snapshots s WHERE s.volume_id = v.volume_id) AS snapshot_count
        FROM volumes v
        LEFT JOIN disks d ON d.disk_id = v.disk_id
        ORDER BY v.volume_id
        """
    ).fetchall()
    return {"items": [_rowget(r) for r in rows], "count": len(rows)}


@router.get("/volumes/{volume_id}")
def volume_detail(volume_id: str, request: Request) -> dict:
    """卷详情 + 其全部快照时间线。"""
    state = get_state(request)
    vol = state.catalog.execute(
        """
        SELECT v.*, d.physical_model, d.physical_serial,
               d.capacity_bytes AS disk_capacity_bytes, d.nickname AS disk_nickname
        FROM volumes v LEFT JOIN disks d ON d.disk_id = v.disk_id
        WHERE v.volume_id = ?
        """,
        (volume_id,),
    ).fetchone()
    if vol is None:
        raise HTTPException(status_code=404, detail=f"卷不存在：{volume_id}")
    snaps = state.catalog.execute(
        """
        SELECT snapshot_id, batch_id, collected_at, status, hash_policy,
               file_count, dir_count, total_bytes, max_depth, skipped_count
        FROM snapshots WHERE volume_id = ? ORDER BY collected_at
        """,
        (volume_id,),
    ).fetchall()
    detail = _rowget(vol)
    detail["snapshots"] = [_rowget(r) for r in snaps]
    return detail


@router.get("/diffs")
def list_diffs(
    request: Request,
    limit: int = 50,
    cursor: "str | None" = None,
) -> dict:
    """历史 diff 列表（服务端 keyset 分页）：created_at 倒序 + rowid 稳定次序键。

    `?limit=<1..200>`（默认 50）+ `?cursor=<不透明>`；响应
    `{items, has_more, next_cursor}`；每条 item 附 `labels: {a, b}`。
    """
    from .routes_snapshots import display_label

    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400,
                            detail="limit 须在 1..200 之间")
    state = get_state(request)
    params: "list[Any]" = []
    where = ""
    if cursor:
        try:
            key, last_id = decode_cursor(cursor)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        where = " WHERE (created_at < ?) OR (created_at = ? AND rowid < ?)"
        params = [key, key, last_id]
    sql = ("SELECT rowid AS _rowid, * FROM diff_runs" + where +
           " ORDER BY created_at DESC, rowid DESC LIMIT ?")
    params.append(limit + 1)  # 多取一行判 has_more，避免空尾页
    rows = state.catalog.execute(sql, params).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]

    # labels 一次取全（避免每行 N+1）：卷昵、盘昵、快照→卷 三幅映射
    vnick = {r[0]: r[1] for r in state.catalog.execute(
        "SELECT volume_id, nickname FROM volumes")}
    dnick = {r[0]: r[1] for r in state.catalog.execute(
        "SELECT disk_id, nickname FROM disks")}
    vol_of = {r[0]: r[1] for r in state.catalog.execute(
        "SELECT snapshot_id, volume_id FROM snapshots")}
    disk_of = {r[0]: r[1] for r in state.catalog.execute(
        "SELECT volume_id, disk_id FROM volumes")}

    def _label(sid: "str | None") -> str:
        vol = vol_of.get(sid or "") or ""
        return display_label(vnick.get(vol), dnick.get(disk_of.get(vol) or ""), vol) \
            if vol else ""

    import json

    items = []
    for r in rows:
        item = _rowget(r)
        rowid = item.pop("_rowid")
        for key2 in ("options_json", "summary_json", "planned_volumes_json"):
            if item.get(key2):
                try:
                    item[key2.removesuffix("_json")] = json.loads(item.pop(key2))
                except json.JSONDecodeError:
                    item.pop(key2)
        item["labels"] = {"a": _label(item.get("a")), "b": _label(item.get("b"))}
        items.append(item)

    out: dict = {"items": items, "has_more": has_more, "next_cursor": None}
    if has_more and rows:
        out["next_cursor"] = encode_cursor(items[-1]["created_at"],
                                           rows[-1]["_rowid"])
    return out


@router.get("/settings")
def get_settings(request: Request) -> dict:
    """服务设置：数据根目录与版本（Web UI 首页展示用）。"""
    from .. import __version__

    state = get_state(request)
    return {"data_root": str(state.data_root), "version": __version__}
