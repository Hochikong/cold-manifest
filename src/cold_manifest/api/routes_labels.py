"""昵称 API（P1）+ 磁盘身份修正入口：磁盘 / 分区的用户昵称（速记 + 比对辨认）。

昵称只存 catalog（disks.nickname / volumes.nickname），采集与 rebuild 永不覆盖。
PATCH 语义：设值 / 更新；空串或 null = 清除；最长 64 字符。

PATCH /api/disks/{disk_id} 同时是"已注册磁盘的身份"的 Web 修正/确认入口
（nickname / identity_verified / physical_serial / disk_serial 可单独或同时传）：

- 安全边界：这里**绝不**破坏快照关联——disk_id / volume_id 是快照的根锚点，
  有快照的盘不允许改 disk_serial（会失去与卷 ID 的派生关系）；"改名"只能走
  删除快照后重采。disk_serial 仅在该盘没有任何快照时允许改（此时级联更新
  disk_id / volumes.disk_id / volume_id 前缀 / disk_smart.disk_id，属"纠正
  录入信息"的安全操作）；
- physical_serial 是记录性字段（信息 + 匹配用），可直接改，仅记审计；
- identity_verified 手动置 true/false 均记 identity_verified_source='manual'
  与 identity_verified_at（UTC ISO）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..catalog import validate_volume_id
from .state import get_state

router = APIRouter(prefix="/api", tags=["labels"])

_NICKNAME_MAX = 64
_SERIAL_MAX = 128


class NicknameBody(BaseModel):
    nickname: "str | None" = None


class DiskPatchBody(BaseModel):
    """PATCH /api/disks/{disk_id} 请求体：全部字段可选，可单独/组合传。

    空串语义与 nickname 一致：清除该字段（disk_serial 例外——空串视为
    忽略/无操作，因为"清除序列号"没有意义）。
    """

    nickname: "str | None" = None
    identity_verified: "bool | None" = None
    physical_serial: "str | None" = None
    disk_serial: "str | None" = None


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


def _normalize_serial(value: "str | None", label: str,
                      allow_clear: bool = True) -> "str | None":
    """规范化序列号：空白 → None；超长 → 400。allow_clear=False 时空串视为忽略。"""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None if allow_clear else False  # type: ignore[return-value]
    if len(text) > _SERIAL_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"{label}过长（最多 {_SERIAL_MAX} 字符，当前 {len(text)}）")
    return text


def _disk_response(state: Any, disk_id: str) -> dict:
    """更新后的磁盘行 + 派生身份状态（PATCH 与 recheck 共用的返回形状）。"""
    from ..identity_audit import disk_identity_status

    row = state.catalog.execute(
        "SELECT * FROM disks WHERE disk_id=?", (disk_id,)).fetchone()
    if row is None:  # 理论不可达（调用方已保证）
        raise HTTPException(status_code=404, detail=f"盘不存在：{disk_id}")
    out = dict(row)
    status, reason = disk_identity_status(state.catalog, state.data_root, disk_id)
    out["identity_status"] = status
    out["identity_status_reason"] = reason
    return out


def _set_identity_verified(state: Any, disk_id: str, verified: bool,
                           source: str) -> str:
    """写 identity_verified 三元组，返回时间戳。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state.catalog.execute(
        "UPDATE disks SET identity_verified=?, identity_verified_source=?,"
        " identity_verified_at=? WHERE disk_id=?",
        (1 if verified else 0, source, now, disk_id))
    return now


@router.patch("/disks/{disk_id}")
def patch_disk(disk_id: str, body: DiskPatchBody, request: Request) -> dict:
    """修正/确认已注册磁盘的身份信息；未知 disk_id → 404，非法值 → 400。

    - nickname：设值/清除（空串或 null = 清除）；
    - identity_verified：手动确认/否认；true→1、false→0，
      同时记 identity_verified_source='manual' 与 identity_verified_at；
    - physical_serial：直接可改（记录性字段，信息+匹配用）；
    - disk_serial：仅该盘**没有任何快照**时允许改——它是 disk_id / 卷 ID 的
      来源，有快照时改序列号会让卷 ID 与快照失联 → 400；无快照时级联更名
      （disks.disk_id / volumes.disk_id / volume_id 前缀 / disk_smart），
      属"纠正录入信息"。改名（把一块盘并到另一块盘名下）不走这里，走删除重采。

    这里**绝不**触碰有快照盘的 disk_id / volume_id（会破坏快照关联）。
    返回更新后的磁盘行 + identity_status / identity_status_reason
    + identity_verified_source / identity_verified_at。
    """
    state = get_state(request)
    row = state.catalog.execute(
        "SELECT * FROM disks WHERE disk_id=?", (disk_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"盘不存在：{disk_id}")

    fields_set = body.model_fields_set
    updates: "dict[str, object]" = {}

    if "nickname" in fields_set:
        updates["nickname"] = _normalize(body.nickname)
    if "physical_serial" in fields_set:
        updates["physical_serial"] = _normalize_serial(
            body.physical_serial, "物理序列号")
    if "disk_serial" in fields_set:
        new_serial = _normalize_serial(body.disk_serial, "磁盘序列号",
                                       allow_clear=False)
        if new_serial is False:  # 空串/空白：忽略
            new_serial = None
        if new_serial and new_serial != disk_id:
            snap_n = state.catalog.execute(
                """
                SELECT COUNT(*) FROM snapshots s
                JOIN volumes v ON v.volume_id = s.volume_id
                WHERE v.disk_id = ?
                """,
                (disk_id,)).fetchone()[0]
            if snap_n:
                raise HTTPException(status_code=400, detail=(
                    f"该盘已有 {snap_n} 个快照，改序列号会让卷 ID 与快照失联；"
                    "请改用「重新校验」或「手动确认」，或删除快照后重采"))
            clash = state.catalog.execute(
                "SELECT 1 FROM disks WHERE disk_id=?", (new_serial,)).fetchone()
            if clash:
                raise HTTPException(
                    status_code=400,
                    detail=f"已存在同序列号的盘记录：{new_serial}（不能合并两条盘记录；"
                           "如需合并请删除其中一块后重采）")
            updates["disk_serial"] = new_serial
    if "identity_verified" in fields_set and body.identity_verified is not None:
        _set_identity_verified(state, disk_id, bool(body.identity_verified),
                               "manual")

    # 级联更名（仅无快照时才会走到这里）：disks / volumes / disk_smart
    new_serial = updates.pop("disk_serial", None)
    if new_serial and new_serial != disk_id:
        old = disk_id
        state.catalog.execute(
            "UPDATE disks SET disk_id=? WHERE disk_id=?", (new_serial, old))
        state.catalog.execute(
            "UPDATE volumes SET disk_id=? WHERE disk_id=?",
            (new_serial, old))
        # volume_id 前缀 {old}_P{n} → {new}_P{n}（LIKE 转义下划线）
        state.catalog.execute(
            "UPDATE volumes SET volume_id = ? || substr(volume_id, ?)"
            " WHERE disk_id=? AND volume_id LIKE ? ESCAPE '\\'",
            (new_serial, len(old) + 1, old, old.replace("_", r"\_") + r"\_%"))
        state.catalog.execute(
            "UPDATE disk_smart SET disk_id=? WHERE disk_id=?",
            (new_serial, old))
        disk_id = new_serial

    for k, v in updates.items():
        state.catalog.execute(
            f"UPDATE disks SET {k}=? WHERE disk_id=?", (v, disk_id))
    state.catalog.commit()
    return _disk_response(state, disk_id)


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
