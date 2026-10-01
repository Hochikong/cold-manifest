"""运维端点（P4.5-fix-60）：catalog 重建（POST /api/admin/rebuild-catalog）。

同步执行 ``rebuild.rebuild_catalog``：扫描 data/*/*/snapshot.db 的 meta，
补建/回填 catalog 的 disks / volumes / snapshots / on_disk_copies 行。
重建写 catalog，必须持 ``DataRootLock``（被占用 → 409）。
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..lockfile import DataRootLock, LockBusy
from ..rebuild import RebuildError, rebuild_catalog
from .state import get_state

router = APIRouter(prefix="/api/admin", tags=["admin"])


class RebuildBody(BaseModel):
    dry_run: bool = False


@router.post("/rebuild-catalog")
def rebuild_catalog_endpoint(body: RebuildBody, request: Request) -> dict:
    """重建/回填 catalog 注册行（持数据根写锁；dry_run=true 只输出计划不写）。"""
    state = get_state(request)
    try:
        with DataRootLock(state.data_root):
            summary: dict = rebuild_catalog(state.data_root, dry_run=body.dry_run)
    except LockBusy as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except RebuildError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    return {**summary, "dry_run": body.dry_run}
