"""昵称 API（P1）：磁盘 / 分区的用户昵称（速记 + 比对辨认）。

昵称只存 catalog（disks.nickname / volumes.nickname），采集与 rebuild 永不覆盖。
PATCH 语义：设值 / 更新；空串或 null = 清除；最长 64 字符。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..catalog import validate_volume_id
from .state import get_state

router = APIRouter(prefix="/api", tags=["labels"])

_NICKNAME_MAX = 64


class NicknameBody(BaseModel):
    nickname: "str | None" = None


def _normalize(value: "str | None") -> "str | None":
    """规范化昵称：None/空白 → None（清除）；超长 → 400。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > _NICKNAME_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"昵称过长（最多 {_NICKNAME_MAX} 字符，当前 {len(text)}）")
    return text


@router.patch("/disks/{disk_id}")
def set_disk_nickname(disk_id: str, body: NicknameBody, request: Request) -> dict:
    """设置/清除磁盘昵称；未知 disk_id → 404。返回更新后的对象。"""
    state = get_state(request)
    nickname = _normalize(body.nickname)
    if state.catalog.execute(
            "SELECT 1 FROM disks WHERE disk_id=?", (disk_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail=f"盘不存在：{disk_id}")
    state.catalog.execute(
        "UPDATE disks SET nickname=? WHERE disk_id=?", (nickname, disk_id))
    state.catalog.commit()
    return {"disk_id": disk_id, "nickname": nickname}


@router.patch("/volumes/{volume_id}")
def set_volume_nickname(volume_id: str, body: NicknameBody, request: Request) -> dict:
    """设置/清除分区昵称；volume_id 非法 → 400，未知卷 → 404。"""
    state = get_state(request)
    try:
        validate_volume_id(volume_id)
    except Exception as e:  # noqa: BLE001 — LegacyImportError 包装为 400
        raise HTTPException(status_code=400, detail=str(e)) from None
    nickname = _normalize(body.nickname)
    if state.catalog.execute(
            "SELECT 1 FROM volumes WHERE volume_id=?", (volume_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail=f"卷不存在：{volume_id}")
    state.catalog.execute(
        "UPDATE volumes SET nickname=? WHERE volume_id=?", (nickname, volume_id))
    state.catalog.commit()
    return {"volume_id": volume_id, "nickname": nickname}


# ---------------------------------------------------------------- 删除（不允许级联删快照）


@router.delete("/disks/{disk_id}")
def delete_disk(disk_id: str, request: Request) -> dict:
    """删除磁盘（连同其全部卷行）；其下存在快照 → 409，不允许级联删快照。

    404=盘不存在；409={message, snapshots, diffs}，message 给可执行指引。
    """
    from ..catalog import ObjectDeleteBlocked, delete_disk as _delete_disk

    state = get_state(request)
    try:
        result = _delete_disk(state.catalog, state.data_root, disk_id)
    except LookupError as e:
        state.catalog.rollback()
        raise HTTPException(status_code=404, detail=str(e)) from None
    except ObjectDeleteBlocked as e:
        state.catalog.rollback()
        raise HTTPException(status_code=409, detail={
            "message": str(e), "snapshots": e.snapshots, "diffs": e.diffs,
        }) from None
    return result


@router.delete("/volumes/{volume_id}")
def delete_volume(volume_id: str, request: Request) -> dict:
    """删除卷；其下存在快照 → 409，不允许级联删快照。

    400=volume_id 非法；404=卷不存在；409={message, snapshots, diffs}。
    """
    from ..catalog import (ObjectDeleteBlocked, SnapshotDeleteError,
                           delete_volume as _delete_volume)
    from ..import_legacy import LegacyImportError

    state = get_state(request)
    try:
        validate_volume_id(volume_id)
    except LegacyImportError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None
    try:
        result = _delete_volume(state.catalog, state.data_root, volume_id)
    except LookupError as e:
        state.catalog.rollback()
        raise HTTPException(status_code=404, detail=str(e)) from None
    except SnapshotDeleteError as e:
        state.catalog.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from None
    except ObjectDeleteBlocked as e:
        state.catalog.rollback()
        raise HTTPException(status_code=409, detail={
            "message": str(e), "snapshots": e.snapshots, "diffs": e.diffs,
        }) from None
    return result
