"""volumes / disks 概览与 diffs 历史端点（§6.2，P0 范围内只读部分）。"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

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
               d.physical_model, d.physical_serial, d.capacity_bytes AS disk_capacity_bytes,
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
        SELECT v.*, d.physical_model, d.physical_serial, d.capacity_bytes AS disk_capacity_bytes
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
def list_diffs(request: Request) -> dict:
    """历史 diff 列表（读 catalog.diff_runs；尚未跑过 diff 时为空）。"""
    state = get_state(request)
    rows = state.catalog.execute(
        "SELECT * FROM diff_runs ORDER BY created_at DESC"
    ).fetchall()
    import json

    items = []
    for r in rows:
        item = _rowget(r)
        for key in ("options_json", "summary_json", "planned_volumes_json"):
            if item.get(key):
                try:
                    item[key.removesuffix("_json")] = json.loads(item.pop(key))
                except json.JSONDecodeError:
                    item.pop(key)
        items.append(item)
    return {"items": items, "count": len(items)}


@router.get("/settings")
def get_settings(request: Request) -> dict:
    """服务设置：数据根目录与版本（Web UI 首页展示用）。"""
    from .. import __version__

    state = get_state(request)
    return {"data_root": str(state.data_root), "version": __version__}
