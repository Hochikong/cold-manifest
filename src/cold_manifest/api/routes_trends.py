"""卷级趋势端点：同一块盘历次 sealed 快照的规模变化（P2-C）。"""

from fastapi import APIRouter, HTTPException, Query, Request

from ..catalog import validate_volume_id
from .state import get_state

router = APIRouter(prefix="/api/volumes", tags=["volumes"])

_LIMIT_DEFAULT = 100
_LIMIT_MAX = 1000


@router.get("/{volume_id}/trends")
def volume_trends(
    volume_id: str,
    request: Request,
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1),
) -> dict:
    """按 collect_time 升序返回该卷历次 sealed 快照的规模指标。"""
    if limit > _LIMIT_MAX:
        raise HTTPException(status_code=400, detail=f"limit 最大 {_LIMIT_MAX}")
    try:
        validate_volume_id(volume_id)
    except Exception:
        raise HTTPException(status_code=400, detail=f"非法 volume_id：{volume_id!r}")
    state = get_state(request)
    vol = state.catalog.execute(
        "SELECT volume_id FROM volumes WHERE volume_id = ?", (volume_id,)
    ).fetchone()
    if vol is None:
        raise HTTPException(status_code=404, detail=f"卷不存在：{volume_id}")
    rows = state.catalog.execute(
        """
        SELECT snapshot_id, collected_at, status, file_count, dir_count,
               total_bytes, max_depth
        FROM snapshots
        WHERE volume_id = ? AND status = 'sealed'
        ORDER BY collected_at
        LIMIT ?
        """,
        (volume_id, limit),
    ).fetchall()
    items = [
        {
            "snapshot_id": r["snapshot_id"],
            "collect_time": r["collected_at"],
            "status": r["status"],
            "file_count": r["file_count"],
            "dir_count": r["dir_count"],
            "total_bytes": r["total_bytes"],
            "max_depth": r["max_depth"],
        }
        for r in rows
    ]
    return {"volume_id": volume_id, "items": items}
