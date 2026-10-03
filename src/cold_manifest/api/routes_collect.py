"""采集任务端点（P1.1b）：POST /api/collect 提交采集任务。

任务执行走 TaskRunner（kind='collect'）；进度/取消/详情复用 /api/tasks。
"""

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..collect import CollectResult, collect_volume, preflight_serial_required
from ..lockfile import DataRootLock, LockBusy
from ..probe import enumerate_disk_volumes
from ..tasks import TaskRunner, register_task_fn
from .state import get_state

router = APIRouter(prefix="/api/collect", tags=["collect"])
batches_router = APIRouter(prefix="/api/batches", tags=["collect"])

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
    resume: bool = False
    cross_filesystems: bool = False
    all_partitions: bool = False


def _run_collect(payload: dict, progress_cb: Any, cancel_event: Any = None) -> dict:
    """工作线程执行体（经 tasks._FN_REGISTRY 调用）。取消时抛 CollectCancelled
    → TaskRunner 落终态 cancelled；引擎负责清理未封库目录。

    执行期间持有 data_root 写锁：与其他写者（CLI collect/清扫）互斥；
    占用时任务失败，错误信息含"data_root 被占用"。"""
    from ..collect import CollectError

    lock = DataRootLock(payload["data_root"])
    try:
        lock.acquire()
    except LockBusy as e:
        raise CollectError(f"data_root 被占用，无法采集：{e}") from e
    try:
        result: CollectResult = collect_volume(
            payload["path"], data_root=payload["data_root"],
            volume_id=payload.get("volume_id") or None,
            manual_serial=payload.get("serial") or None,
            exclude_globs=payload.get("exclude_globs") or None,
            exclude_hidden=bool(payload.get("exclude_hidden")),
            include_system=bool(payload.get("include_system", True)),
            smartctl=bool(payload.get("smartctl", True)),
            on_disk_copy=bool(payload.get("on_disk_copy", True)),
            resume=bool(payload.get("resume", False)),
            cross_filesystems=bool(payload.get("cross_filesystems", False)),
            cancel_event=cancel_event, progress_cb=progress_cb,
        )
    finally:
        lock.release()
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


def _collect_payload(body: CollectBody, target_path: str, data_root: str) -> dict:
    return {
        "path": target_path,
        "volume_id": body.volume_id,
        "serial": body.serial,
        "exclude_globs": list(body.exclude_globs),
        "exclude_hidden": body.exclude_hidden,
        "include_system": body.include_system,
        "smartctl": body.smartctl,
        "on_disk_copy": body.on_disk_copy,
        "resume": body.resume,
        "cross_filesystems": body.cross_filesystems,
        "data_root": data_root,
    }


@router.post("", status_code=201)
def submit_collect(body: CollectBody, request: Request) -> dict:
    p = Path(body.path)
    if not p.is_absolute() or not p.is_dir():
        raise HTTPException(status_code=400, detail=f"path 不存在或不是目录：{body.path}")

    runner: TaskRunner = request.app.state.task_runner
    resolved = str(p.resolve())
    data_root = str(get_state(request).data_root)

    # 手填序列号预检（硬盘盒/占位序列号）：不填直接 400 拒绝提交，
    # detail 为人话提示（前端据此高亮"磁盘序列号"字段）。
    # 批次（--all-partitions）是同一块盘的多个卷，probe 一次即可判定。
    _msg = preflight_serial_required(resolved, manual_serial=body.serial,
                                     smartctl=body.smartctl)
    if _msg:
        raise HTTPException(status_code=400, detail=_msg)

    if body.all_partitions:
        return _submit_collect_batch(body, runner, resolved, data_root)

    payload = _collect_payload(body, resolved, data_root)
    # 提交去重（check+insert 在锁内完成，消除并发提交竞态）；None=已有同路径任务
    task_id = runner.submit_dedup(_KIND_COLLECT, payload, field="path")
    if task_id is None:
        raise HTTPException(status_code=409,
                            detail=f"同路径已有 pending/running 的采集任务：{resolved}")
    return {"task_id": task_id, "status": "pending"}


def _submit_collect_batch(body: CollectBody, runner: TaskRunner,
                          resolved: str, data_root: str) -> dict:
    """多分区采集批次（§4.2）：枚举 path 所在盘的全部卷 → 每卷一个 collect
    任务（related_id=batch_id）→ 串行执行（单工作线程 FIFO）。

    部分失败=逐任务终态、批次 partial；单卷重试=终态后再 POST 该卷 path
    （409 去重自动解除）。任一卷与其他活跃任务路径冲突 → 整批 409。
    """
    targets, warnings = enumerate_disk_volumes(resolved)
    if not targets:
        raise HTTPException(status_code=400,
                            detail=f"{resolved} 所在盘没有可采集的卷")

    # 先做整批去重预检：任一卷冲突则整批拒绝（避免留下一批半成品任务）
    for t in targets:
        if runner.has_active_for(t.path, kind=_KIND_COLLECT, field="path"):
            raise HTTPException(
                status_code=409,
                detail=f"卷 {t.path} 已有 pending/running 的采集任务，整批拒绝")

    batch_id = f"batch_{uuid.uuid4().hex[:12]}"
    disk_id = ""  # 多批次行不锚定单 disk/volume_id（采集时逐卷生成）
    planned = [
        {"path": t.path, "device_path": t.device_path,
         "partition_index": t.partition_index, "filesystem": t.filesystem,
         "label": t.label, "capacity_bytes": t.capacity_bytes}
        for t in targets
    ]
    runner.create_batch_row(batch_id, disk_id, resolved, planned)

    task_ids: list[str] = []
    try:
        for t in targets:
            payload = _collect_payload(body, t.path, data_root)
            task_id = runner.submit_dedup(_KIND_COLLECT, payload, field="path",
                                          related_id=batch_id)
            if task_id is None:  # 预检后竞态冲突：极小概率，整批报 409
                raise HTTPException(
                    status_code=409,
                    detail=f"卷 {t.path} 已有 pending/running 的采集任务，整批拒绝")
            task_ids.append(task_id)
    except HTTPException:
        # 提交原子性：清掉刚建的 batch 行与 pending 子任务，避免半成品残留
        runner.delete_batch_and_tasks(batch_id)
        raise
    return {"batch_id": batch_id, "task_ids": task_ids,
            "planned_volumes": planned, "warnings": warnings}


@batches_router.get("/{batch_id}")
def batch_detail(batch_id: str, request: Request) -> dict:
    runner: TaskRunner = request.app.state.task_runner
    batch = runner.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"批次不存在：{batch_id}")
    return batch
