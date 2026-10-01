"""导入相关端点（P1.0）：扫描 v1 快照目录 + 提交后台导入任务。"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..import_legacy import ImportResult, import_snapshot
from ..tasks import TaskRunner, register_task_fn
from .state import get_state

router = APIRouter(prefix="/api/imports", tags=["imports"])

# 任务类型名（tasks.kind）；fn 在模块导入时注册
_KIND_IMPORT = "import"


@dataclass
class _Candidate:
    snapshot_dir: Path
    volume_id: str
    collect_time: str
    tree_csv_bytes: int


def _read_meta(snapshot_dir: Path) -> "dict[str, str] | None":
    """读 metadata.csv 为 dict；缺失/为空返回 None。"""
    path = snapshot_dir / "metadata.csv"
    if not path.is_file():
        return None
    import csv
    meta: dict = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row) >= 2 and row[0] != "metric_name":
                meta[row[0]] = row[1]
    return meta or None


def _iter_v1_dirs(root: Path) -> "tuple[list[Path], list[str]]":
    """root 自身及一级子目录中的 v1 快照目录（含 metadata.csv + tree.csv）。

    迭代中的 OSError（权限/IO）不冒泡，收进 errors 列表。
    """
    dirs: list[Path] = []
    errors: list[str] = []
    try:
        children = sorted(root.iterdir())
    except OSError as e:
        errors.append(f"{root}: {e}")
        children = []
    for d in [root, *children]:
        try:
            if d.is_dir() and (d / "metadata.csv").is_file() and (d / "tree.csv").is_file():
                dirs.append(d)
        except OSError as e:
            errors.append(f"{d}: {e}")
    return dirs, errors


def _scan_dir(root: Path, catalog: Any) -> "tuple[list[dict], list[str]]":
    candidates: list[dict] = []
    errors: list[str] = []
    v1_dirs, iter_errors = _iter_v1_dirs(root)
    errors.extend(iter_errors)
    for d in v1_dirs:
        try:
            meta = _read_meta(d)
            if meta is None:
                continue
            candidates.append({
                "snapshot_dir": str(d.resolve()),
                "volume_id": meta.get("volume_id") or "",
                "collect_time": meta.get("collect_time_utc") or "",
                "tree_csv_bytes": (d / "tree.csv").stat().st_size,
                "status": "imported" if _already_imported(catalog, d) else "new",
            })
        except OSError as e:
            errors.append(f"{d}: {e}")
    return candidates, errors


def _already_imported(catalog: Any, snapshot_dir: Path) -> bool:
    """同 host_path 的 sealed 快照已存在 → imported。"""
    row = catalog.execute(
        "SELECT 1 FROM snapshots WHERE host_path=? AND status='sealed' LIMIT 1",
        (str(snapshot_dir.resolve()),),
    ).fetchone()
    return row is not None


def _allowed_by_roots(p: Path) -> bool:
    """CLDM_IMPORT_ROOTS 白名单校验；未设置视为允许。

    分隔符用 os.pathsep（Windows 上为 ';'，兼容盘符路径中的冒号）。
    """
    roots = os.environ.get("CLDM_IMPORT_ROOTS", "").strip()
    if not roots:
        return True
    resolved = p.resolve()
    allowed = [Path(r).resolve() for r in roots.split(os.pathsep) if r.strip()]
    return any(resolved == a or a in resolved.parents for a in allowed)


@router.get("/scan")
def scan(path: str, request: Request) -> dict:
    """扫描 path 自身及一级子目录，找 v1 快照目录。"""
    p = Path(path)
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail=f"path 必须为绝对路径：{path}")
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"path 不存在或不是目录：{path}")

    # 可选白名单：CLDM_IMPORT_ROOTS（os.pathsep 分隔）设了才启用前缀校验
    if not _allowed_by_roots(p):
        raise HTTPException(status_code=400, detail=f"path 不在允许的导入根内：{path}")

    state = get_state(request)
    candidates, errors = _scan_dir(p, state.catalog)
    return {"root": str(p), "candidates": candidates, "errors": errors}


class ImportBody(BaseModel):
    snapshot_dir: str
    volume_id: "str | None" = None
    force: bool = False


def _run_import(payload: dict, progress_cb: Any, cancel_event: Any = None) -> dict:
    """工作线程执行体（经 tasks._FN_REGISTRY 调用）。"""
    result: ImportResult = import_snapshot(
        payload["snapshot_dir"], payload["data_root"],
        volume_id=payload.get("volume_id") or None,
        force=bool(payload.get("force")), progress_cb=progress_cb,
    )
    return {"snapshot_id": result.snapshot_id, "volume_id": result.volume_id,
            "skipped_import": result.skipped_import}


register_task_fn(_KIND_IMPORT, _run_import)


@router.post("", status_code=201)
def submit_import(body: ImportBody, request: Request) -> dict:
    d = Path(body.snapshot_dir)
    if not d.is_absolute() or not d.is_dir():
        raise HTTPException(status_code=400, detail=f"无效快照目录：{body.snapshot_dir}")
    if not (d / "metadata.csv").is_file() or not (d / "tree.csv").is_file():
        raise HTTPException(status_code=400,
                            detail="目录缺少 metadata.csv 或 tree.csv，不是 v1 快照目录")

    # 白名单（同 scan）
    if not _allowed_by_roots(d):
        raise HTTPException(status_code=400, detail=f"path 不在允许的导入根内：{body.snapshot_dir}")

    runner: TaskRunner = request.app.state.task_runner

    payload = {"snapshot_dir": str(d.resolve()), "volume_id": body.volume_id,
               "force": body.force, "data_root": str(get_state(request).data_root)}
    # 提交去重（check+insert 锁内完成）；None=已有同目录任务
    task_id = runner.submit_dedup(_KIND_IMPORT, payload, field="snapshot_dir")
    if task_id is None:
        raise HTTPException(status_code=409,
                            detail="同目录已有 pending/running 的导入任务")
    return {"task_id": task_id, "status": "pending"}


class ImportDbBody(BaseModel):
    path: str


@router.post("/db")
def import_db(body: ImportDbBody, request: Request, response: Response) -> dict:
    """就地登记外部 snapshot.db（复用 import_db.import_snapshot_db）。

    路径须在 CLDM_IMPORT_ROOTS 白名单内（设置时）；未封库 / 不存在 /
    无法推导 <volume_id>/<ts> 布局 → 400；已登记 → 200 幂等（created=false）。
    """
    from ..import_db import ImportDbError, import_snapshot_db

    p = Path(body.path)
    if not p.is_absolute():
        raise HTTPException(status_code=400,
                            detail=f"path 必须为绝对路径：{body.path}")
    if not p.is_file():
        raise HTTPException(status_code=400,
                            detail=f"快照库不存在或不是文件：{body.path}")
    if not _allowed_by_roots(p):
        raise HTTPException(status_code=400,
                            detail=f"path 不在允许的导入根内：{body.path}")

    state = get_state(request)
    try:
        result = import_snapshot_db(p, state.data_root)
    except ImportDbError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None

    row = state.catalog.execute(
        "SELECT * FROM snapshots WHERE snapshot_id = ?",
        (result["snapshot_id"],),
    ).fetchone()
    response.status_code = 201 if result["created"] else 200
    return {
        "created": result["created"],
        "snapshot_id": result["snapshot_id"],
        "volume_id": result["volume_id"],
        "db_path": str(result["db_path"]),
        "snapshot": dict(row) if row is not None else None,
        "warnings": result["warnings"],
    }
