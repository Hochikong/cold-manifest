"""采集任务端点（P1.1b）：POST /api/collect 提交采集任务。

任务执行走 TaskRunner（kind='collect'）；进度/取消/详情复用 /api/tasks。
"""

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..collect import CollectResult, collect_volume
from ..tasks import TaskRunner, register_task_fn
from .state import get_state

router = APIRouter(prefix="/api/collect", tags=["collect"])

# 任务类型名（tasks.kind）；fn 在模块导入时注册
_KIND_COLLECT = "collect"


class CollectBody(BaseModel):
    path: str
    volume_id: "str | None" = None
    serial: "str | None" = None
    exclude_globs: list[str] = Field(default_factory=list)
    exclude_hidden: bool = False
    include_system: bool = True
    smartctl: bool = True
    on_disk_copy: bool = True


def _run_collect(payload: dict, progress_cb: Any, cancel_event: Any = None) -> dict:
    """工作线程执行体（经 tasks._FN_REGISTRY 调用）。取消时抛 CollectCancelled
    → TaskRunner 落终态 cancelled；引擎负责清理未封库目录。"""
    result: CollectResult = collect_volume(
        payload["path"], data_root=payload["data_root"],
        volume_id=payload.get("volume_id") or None,
        manual_serial=payload.get("serial") or None,
        exclude_globs=payload.get("exclude_globs") or None,
        exclude_hidden=bool(payload.get("exclude_hidden")),
        include_system=bool(payload.get("include_system", True)),
        smartctl=bool(payload.get("smartctl", True)),
        on_disk_copy=bool(payload.get("on_disk_copy", True)),
        cancel_event=cancel_event, progress_cb=progress_cb,
    )
    return {
        "snapshot_id": result.snapshot_id,
        "volume_id": result.volume_id,
        "files": result.files,
        "dirs": result.dirs,
        "total_bytes": result.total_bytes,
        "skipped": result.skipped,
        "elapsed_s": round(result.elapsed_s, 3),
        "warnings": result.warnings,
    }


register_task_fn(_KIND_COLLECT, _run_collect)


@router.post("", status_code=201)
def submit_collect(body: CollectBody, request: Request) -> dict:
    p = Path(body.path)
    if not p.is_absolute() or not p.is_dir():
        raise HTTPException(status_code=400, detail=f"path 不存在或不是目录：{body.path}")

    runner: TaskRunner = request.app.state.task_runner
    resolved = str(p.resolve())

    payload = {
        "path": resolved,
        "volume_id": body.volume_id,
        "serial": body.serial,
        "exclude_globs": list(body.exclude_globs),
        "exclude_hidden": body.exclude_hidden,
        "include_system": body.include_system,
        "smartctl": body.smartctl,
        "on_disk_copy": body.on_disk_copy,
        "data_root": str(get_state(request).data_root),
    }
    # 提交去重（check+insert 在锁内完成，消除并发提交竞态）；None=已有同路径任务
    task_id = runner.submit_dedup(_KIND_COLLECT, payload, field="path")
    if task_id is None:
        raise HTTPException(status_code=409,
                            detail=f"同路径已有 pending/running 的采集任务：{resolved}")
    return {"task_id": task_id, "status": "pending"}
