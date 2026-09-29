"""任务端点：列表（keyset 分页）、详情、取消、SSE 进度流。"""

import base64
import json
import time
from typing import Iterator, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..tasks import TaskRunner

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_LIMIT_DEFAULT = 50
_LIMIT_MAX = 500

# SSE 默认推送间隔与上下限（?interval_ms= 可调）
_SSE_INTERVAL_DEFAULT_MS = 500
_SSE_INTERVAL_MIN_MS = 100
_SSE_INTERVAL_MAX_MS = 5000

_TERMINAL_STATUSES = ("done", "error", "cancelled")


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
    status: "Literal['pending','running','done','error','cancelled'] | None" = Query(default=None),
    batch_id: "str | None" = Query(default=None),
) -> dict:
    runner: TaskRunner = request.app.state.task_runner
    c_at = c_id = None
    if cursor:
        c_at, c_id = _decode_task_cursor(cursor)
    items, next_cursor, has_more = runner.list_tasks(
        limit, cursor_created_at=c_at, cursor_id=c_id, status=status,
        related_id=batch_id)
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


@router.get("/{task_id}")
def task_detail(task_id: str, request: Request) -> dict:
    runner: TaskRunner = request.app.state.task_runner
    task = runner.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    return task


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, request: Request) -> dict:
    """取消任务：pending 直接落 cancelled；running 置取消事件（协作取消）。

    返回请求后的任务状态（running → "cancelling"，稍后轮询到 cancelled）。
    """
    runner: TaskRunner = request.app.state.task_runner
    try:
        status = runner.cancel(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}") from None
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return {"status": status}


@router.get("/{task_id}/events")
def task_events(
    task_id: str,
    request: Request,
    interval_ms: int = Query(default=_SSE_INTERVAL_DEFAULT_MS,
                             ge=_SSE_INTERVAL_MIN_MS, le=_SSE_INTERVAL_MAX_MS),
) -> StreamingResponse:
    """SSE 进度流（text/event-stream）：每 interval_ms 推一帧完整 task JSON，
    终态帧推完即关闭。客户端断开由生成器关闭（GeneratorExit）自然处理。"""
    runner: TaskRunner = request.app.state.task_runner
    if runner.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：{task_id}")
    interval_s = interval_ms / 1000.0

    def stream() -> Iterator[str]:
        while True:
            task = runner.get_task(task_id)
            if task is None:  # 并发删除等极端情况：发最后一帧空并退出
                yield "data: null\n\n"
                return
            yield f"data: {json.dumps(task, ensure_ascii=False)}\n\n"
            if task["status"] in _TERMINAL_STATUSES:
                return
            time.sleep(interval_s)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
