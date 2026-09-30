"""重复文件报告端点（P3-A）：GET /api/snapshots/{sid}/duplicates。

注册顺序注意：snapshots 路由里的 GET /{snapshot_id:path} 是吞一切后缀的
catch-all（path converter），本 router 必须在 server.py 中先于它注册，
否则 /duplicates 会被详情端点吞掉。
"""

from fastapi import APIRouter, HTTPException, Query, Request

from ..catalog import find_snapshot
from ..duplicates import (DEFAULT_LIMIT, DuplicatesError, MAX_LIMIT, MODES,
                          find_duplicates)
from .state import get_state

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])


@router.get("/{snapshot_id:path}/duplicates")
def snapshot_duplicates(
    snapshot_id: str,
    request: Request,
    mode: str = Query(default="content",
                      description="查重档位：content=完整哈希（默认）/ name=同名 /"
                                  " fingerprint=大小+抽样指纹"),
    min_size: int = Query(default=1048576, ge=0,
                          description="只统计 ≥ 此大小的文件（默认 1MiB）"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, description="每页组数（默认 100）"),
    cursor: "str | None" = Query(default=None, description="keyset 分页游标"),
) -> dict:
    """快照内重复文件三档查重（响应带 mode 字段；content 档行为与历史完全一致）。

    content：只认 full 哈希（非 full → 400）；name：按文件名 NOCASE 分组，无需哈希；
    fingerprint：按 (size, hash) 分组，纳入 full/sampled（无任何可用哈希 → 400）。
    """
    if mode not in MODES:
        raise HTTPException(status_code=400,
                            detail=f"非法 mode：{mode!r}（允许 {'/'.join(MODES)}）")
    if limit > MAX_LIMIT:
        raise HTTPException(status_code=400, detail=f"limit 上限 {MAX_LIMIT}")
    state = get_state(request)
    if find_snapshot(state.catalog, snapshot_id) is None:
        raise HTTPException(status_code=404, detail=f"快照不存在：{snapshot_id}")
    try:
        conn = state.snapshot_db(snapshot_id)
    except LookupError:
        raise HTTPException(status_code=404,
                            detail=f"快照库文件缺失：{snapshot_id}") from None
    try:
        return find_duplicates(conn, snapshot_id, mode=mode,
                               min_size=min_size, limit=limit, cursor=cursor)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    except DuplicatesError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
