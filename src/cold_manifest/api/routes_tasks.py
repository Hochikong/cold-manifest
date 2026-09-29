"""任务端点（P1.0）：列表（keyset 分页）与详情；前端 1s 轮询。"""

import base64
import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from ..tasks import TaskRunner

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_LIMIT_DEFAULT = 50
_LIMIT_MAX = 500


def _decode_task_cursor(cursor: str) -> "tuple[str, str]":
    try:
        obj = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        return obj["c"], obj["i"]
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="非法 cursor") from None


@router.get("")
def list_tasks(
    request: Request,
    limit: int = Query(default=_LIMIT_DEFAULT, ge=1, le=_LIMIT_MAX),
    cursor: "str | None" = Query(default=None),
    status: "Literal['pending','running','done','error'] | None" = Query(default=None),
) -> dict:
    runner: TaskRunner = request.app.state.task_runner
    c_at = c_id = None
    if cursor:
        c_at, c_id = _decode_task_cursor(cursor)
    items, next_cursor, has_more = runner.list_tasks(
        limit, cursor_created_at=c_at, cursor_id=c_id, status=status)
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


@router.get("/{task_id}")
def task_detail(task_id: str, request: Request) -> dict:
    runner: TaskRunner = request.app.state.task_runner
    task = runner.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return task
