"""磁盘 API（P4-②）：盘列表 / 本机可见盘 / 盘详情 / SMART 历史 / 采集依赖检查。

本机可见盘与 preflight 是唯一有"现场副作用"的只读端点（跑外部命令、写临时
探针文件），失败一律降级为 available:false / warnings，绝不 500。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import smart
from .state import get_state

router = APIRouter(prefix="/api", tags=["disks"])


# ---------------------------------------------------------------- helpers


def _latest_catalog_smart(conn: Any, disk_id: str) -> "dict | None":
    row = conn.execute(
        "SELECT * FROM disk_smart WHERE disk_id=? ORDER BY collected_at DESC LIMIT 1",
        (disk_id,),
    ).fetchone()
    return dict(row) if row else None


def _meta_backfill(state: Any, disk_id: str) -> "list[dict]":
    """读时回填（不写库）：历史表无行但快照库 meta 有 smart_raw_json 的快照，
    现场解析并标 source='meta'。库缺失/解析失败静默跳过。"""
    have = {r["snapshot_id"] for r in state.catalog.execute(
        "SELECT snapshot_id FROM disk_smart WHERE disk_id=?", (disk_id,)).fetchall()}
    rows = state.catalog.execute(
        "SELECT s.snapshot_id, s.collected_at FROM snapshots s"
        " JOIN volumes v ON v.volume_id = s.volume_id WHERE v.disk_id=?"
        " ORDER BY s.collected_at",
        (disk_id,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        sid = r["snapshot_id"]
        if sid in have:
            continue
        try:
            sconn = state.snapshot_db(sid)
            raw_row = sconn.execute(
                "SELECT value FROM meta WHERE key='smart_raw_json'").fetchone()
        except Exception:  # noqa: BLE001 — 库缺失/只读失败跳过
            continue
        raw = raw_row[0] if raw_row else None
        if not raw:
            continue
        item = smart.parse_smart(raw)
        item["snapshot_id"] = sid
        item["collected_at"] = r["collected_at"]
        item["source"] = "meta"
        out.append(item)
    return out


# ---------------------------------------------------------------- 盘列表 / 详情


@router.get("/disks")
def list_disks(request: Request) -> dict:
    """盘列表：身份 + 最近 SMART 摘要 + 卷数/快照数。"""
    state = get_state(request)
    rows = state.catalog.execute(
        """
        SELECT d.disk_id, d.physical_model, d.physical_serial, d.bridge_model,
               d.capacity_bytes, d.interface_type, d.first_seen, d.last_seen, d.nickname,
               (SELECT COUNT(*) FROM volumes v WHERE v.disk_id = d.disk_id) AS volume_count,
               (SELECT COUNT(*) FROM snapshots s JOIN volumes v2 ON s.volume_id = v2.volume_id
                WHERE v2.disk_id = d.disk_id) AS snapshot_count
        FROM disks d ORDER BY d.disk_id
        """
    ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        latest = _latest_catalog_smart(state.catalog, r["disk_id"])
        if latest is None:
            back = _meta_backfill(state, r["disk_id"])
            latest = back[-1] if back else None
        item["latest_smart"] = (
            {k: latest[k] for k in
             ("health", "temperature_c", "power_on_hours", "reallocated_ct",
              "pending_ct", "collected_at")} if latest else None)
        items.append(item)
    return {"items": items, "count": len(items)}


@router.get("/disks/attached")
def disks_attached() -> dict:
    """本机可见盘（复用 probe 枚举）。不可用时不崩：available:false + 原因。"""
    if sys.platform.startswith("linux"):
        try:
            return _attached_linux()
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": str(e), "items": []}
    if sys.platform == "win32":
        try:
            return _attached_win()
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": str(e), "items": []}
    return {"available": False, "reason": f"不支持的平台：{sys.platform}", "items": []}


def _attached_linux() -> dict:
    from ..probe.linux import _mounts_of, _run, parse_lsblk

    proc = _run(["lsblk", "-J", "-b", "-o",
                 "NAME,PATH,TYPE,FSTYPE,LABEL,UUID,SERIAL,MODEL,SIZE,MOUNTPOINTS"])
    nodes = parse_lsblk(proc.stdout)
    items = []
    for d in (n for n in nodes if n.get("type") == "disk"):
        vols = []
        for n in nodes:
            if n.get("parent") is d and n.get("type") == "part":
                for mp in _mounts_of(n):
                    vols.append({
                        "path": mp,
                        "device": n.get("path") or n.get("name") or "",
                        "filesystem": n.get("fstype") or "",
                        "label": n.get("label") or "",
                    })
        items.append({
            "device": d.get("path") or d.get("name") or "",
            "model": (d.get("model") or "").strip(),
            "serial": (d.get("serial") or "").strip(),
            "size_bytes": d.get("size"),
            "volumes": vols,
        })
    return {"available": True, "items": items, "count": len(items)}


_PS_ATTACHED = r"""
$ErrorActionPreference = 'Stop'
$out = @()
foreach ($dd in (Get-CimInstance Win32_DiskDrive)) {
  $vols = @()
  try {
    foreach ($p in (Get-Partition -DiskNumber $dd.Index)) {
      if (-not $p.DriveLetter) { continue }
      $ld = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($p.DriveLetter):'"
      if ($ld) {
        $vols += @{ path = "$($p.DriveLetter):\"; filesystem = $ld.FileSystem;
                     label = $ld.VolumeName }
      }
    }
  } catch {}
  $out += @{ device = "\\.\PhysicalDrive$($dd.Index)"; model = $dd.Model;
             serial = ($dd.SerialNumber -replace '\s+$',''); size_bytes = $dd.Size;
             volumes = $vols }
}
[pscustomobject]@{ items = $out } | ConvertTo-Json -Depth 5
"""


def _attached_win() -> dict:
    from ..probe.windows import _decode

    import subprocess

    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_ATTACHED]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"PowerShell 枚举失败：{exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"PowerShell 枚举失败（rc={proc.returncode}）")
    data = json.loads(_decode(proc.stdout))
    items = data.get("items") or []
    if isinstance(items, dict):  # 单盘时 ConvertTo-Json 不产数组
        items = [items]
    return {"available": True, "items": items, "count": len(items)}


@router.get("/disks/{disk_id}")
def disk_detail(disk_id: str, request: Request) -> dict:
    """盘详情：身份 + 卷列表 + 最近 SMART（catalog 优先，缺则 meta 回填）+ 快照时间线。"""
    state = get_state(request)
    row = state.catalog.execute("SELECT * FROM disks WHERE disk_id=?", (disk_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"盘不存在：{disk_id}")
    detail = dict(row)
    detail["volumes"] = [dict(v) for v in state.catalog.execute(
        "SELECT * FROM volumes WHERE disk_id=? ORDER BY volume_id", (disk_id,)
    ).fetchall()]
    detail["snapshots"] = [dict(s) for s in state.catalog.execute(
        """
        SELECT s.snapshot_id, s.volume_id, s.collected_at, s.status, s.file_count,
               s.dir_count, s.total_bytes, s.skipped_count
        FROM snapshots s JOIN volumes v ON v.volume_id = s.volume_id
        WHERE v.disk_id = ? ORDER BY s.collected_at
        """,
        (disk_id,),
    ).fetchall()]
    latest = _latest_catalog_smart(state.catalog, disk_id)
    if latest is not None:
        latest["source"] = "catalog"
    else:
        back = _meta_backfill(state, disk_id)
        latest = back[-1] if back else None
    detail["latest_smart"] = latest
    detail["smart_error"] = _latest_meta_smart_error(state, disk_id)
    return detail


@router.get("/disks/{disk_id}/smart")
def disk_smart_history(disk_id: str, request: Request) -> dict:
    """SMART 历史序列（collected_at 升序）。catalog 行 source='catalog'；
    老快照 meta 现场解析的行 source='meta'（不写库）。"""
    state = get_state(request)
    if state.catalog.execute(
            "SELECT 1 FROM disks WHERE disk_id=?", (disk_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail=f"盘不存在：{disk_id}")
    items = []
    for r in list_smart_rows(state, disk_id):
        r["source"] = "catalog"
        items.append(r)
    items.extend(_meta_backfill(state, disk_id))
    items.sort(key=lambda x: x.get("collected_at") or "")
    return {"disk_id": disk_id, "items": items, "count": len(items),
            "smart_error": _latest_meta_smart_error(state, disk_id)}


def list_smart_rows(state: Any, disk_id: str) -> "list[dict]":
    return smart.list_smart(state.catalog, disk_id)


def _latest_meta_smart_error(state: Any, disk_id: str) -> "dict | None":
    """该盘最新一个带 meta.smart_error 的快照（前端解释"为什么没有 SMART"）。"""
    rows = state.catalog.execute(
        """
        SELECT s.snapshot_id FROM snapshots s
        JOIN volumes v ON v.volume_id = s.volume_id
        WHERE v.disk_id = ? ORDER BY s.collected_at DESC
        """,
        (disk_id,),
    ).fetchall()
    for r in rows:
        try:
            sconn = state.snapshot_db(r["snapshot_id"])
            pairs = sconn.execute(
                "SELECT key, value FROM meta"
                " WHERE key IN ('smart_error','smart_error_raw')").fetchall()
        except Exception:  # noqa: BLE001 — 库缺失/只读失败跳过
            continue
        d = {k: v for k, v in pairs}
        if d.get("smart_error"):
            return {
                "smart_error": d["smart_error"],
                "smart_error_raw": d.get("smart_error_raw"),
                "snapshot_id": r["snapshot_id"],
            }
    return None


# ---------------------------------------------------------------- 现场读取 SMART


class SmartReadBody(BaseModel):
    path: "str | None" = None


def _disk_index_of(item: dict) -> "int | None":
    """attached 枚举项的 PhysicalDrive 盘号（\\\\.\\PhysicalDriveN → N）。"""
    m = re.search(r"PhysicalDrive(\d+)$", str(item.get("device") or ""))
    return int(m.group(1)) if m else None


def _locate_attached_candidates(disk_id: str, path: "str | None"
                                ) -> "tuple[Any, list[dict]]":
    """定位当前插着的盘的**候选链**：返回 (attached 容量或 None, 候选列表)。

    现场读取必须走候选链（smart_device_candidates），单设备曾导致 USB 桥盘
    永远打不开（PhysicalDrive 形态在桥上常 Invalid argument）。有 path：
    Windows 优先在 attached 枚举里反查盘号走全链，反查不到退回
    smart.device_candidates_for_path；Linux 直接 /proc/mounts 定位。
    无 path：按序列号在 attached 里找。定位不到抛 HTTPException 404。
    """
    attached: "dict | None" = None
    need_attached = path is None or sys.platform == "win32"
    if need_attached:
        if sys.platform.startswith("linux"):
            try:
                attached = _attached_linux()
            except Exception as e:  # noqa: BLE001
                if path is None:
                    raise HTTPException(status_code=404,
                                        detail=f"本机盘枚举失败：{e}")
        elif sys.platform == "win32":
            try:
                attached = _attached_win()
            except Exception as e:  # noqa: BLE001
                if path is None:
                    raise HTTPException(status_code=404,
                                        detail=f"本机盘枚举失败：{e}")
        elif path is None:
            raise HTTPException(status_code=404,
                                detail=f"不支持的平台：{sys.platform}")

    if path:
        want = str(path).rstrip("\\/").lower()
        item = None
        if attached:
            item = next(
                (i for i in attached.get("items") or [] if any(
                    str(v.get("path") or "").rstrip("\\/").lower() == want
                    for v in i.get("volumes") or [])),
                None,
            )
        if item is not None:
            idx = _disk_index_of(item)
            cands = (smart.smart_device_candidates(idx) if idx is not None
                     else smart.device_candidates_for_path(path))
            return item.get("size_bytes"), cands
        cands = smart.device_candidates_for_path(path)
        if not cands:
            raise HTTPException(status_code=404,
                                detail=f"无法定位 {path} 所在物理盘")
        return None, cands

    want = (disk_id or "").strip()
    item = next(
        (i for i in (attached or {}).get("items") or []
         if str(i.get("serial") or "").strip() == want),
        None,
    )
    if item is None:
        raise HTTPException(status_code=404,
                            detail=f"盘 {disk_id} 当前不在线（未在 /api/disks/attached 中）")
    idx = _disk_index_of(item)
    if idx is not None:
        return item.get("size_bytes"), smart.smart_device_candidates(idx)
    dev = str(item.get("device") or "").strip()
    if not dev and item.get("volumes"):
        cands = smart.device_candidates_for_path(
            item["volumes"][0].get("path") or "")
        if cands:
            return item.get("size_bytes"), cands
    if not dev:
        raise HTTPException(status_code=404,
                            detail=f"盘 {disk_id} 在线但无法定位物理设备")
    return item.get("size_bytes"), [{"device": dev, "type": "",
                                     "source": "attached"}]


def _capacity_screen(cands: "list[dict]",
                     expected_bytes: Any) -> "tuple[list[dict], str | None]":
    """扫描映射的容量软校验：--scan 行序与盘号一致只是假设。

    scan 来源候选的实际容量（smartctl -i）与 attached 枚举容量差 >20% 时
    剔除该候选（降级到 /dev/sdN、PhysicalDrive fallback 形态），返回
    (候选, "mismatch")；任一方无容量则不校验，原样返回。
    """
    if not expected_bytes:
        return cands, None
    try:
        expected = int(expected_bytes)
    except (TypeError, ValueError):
        return cands, None
    out: "list[dict]" = []
    status: "str | None" = None
    for c in cands:
        if c.get("source") == "scan":
            cap = smart.device_capacity(c["device"])
            if cap and abs(cap - expected) > 0.2 * max(cap, expected):
                status = "mismatch"
                continue
        out.append(c)
    if not out:  # 全被剔除时保留原候选，宁可试错也不空手
        return cands, status
    return out, status


@router.post("/disks/{disk_id}/smart/read")
def disk_smart_read(disk_id: str, request: Request,
                    body: "SmartReadBody | None" = None) -> dict:
    """对当前插着的盘现场读一次 SMART（不写库）。

    盘不在（404 之外）的读取失败一律 200 + ok=false + 人话原因，绝不 500。
    """
    get_state(request)  # 仅确认服务已挂 data_root
    size_bytes, cands = _locate_attached_candidates(
        disk_id, body.path if body else None)
    size_bytes, cap_status = _capacity_screen(cands, size_bytes)
    if not cands:
        raise HTTPException(status_code=404, detail="无法定位该盘的物理设备候选")
    res = smart.read_smart_verbose(devices=cands)
    if cap_status:
        res.setdefault("scan_info", {})["capacity_check"] = cap_status
    device = res.get("device") or (cands[0]["device"] if cands else "")
    out: dict = {
        "disk_id": disk_id,
        "device": device,
        "device_candidates": res.get("device_candidates"),
        "scan_info": res.get("scan_info"),
        "ok": res["ok"],
        "device_type": res["device_type"],
        "reason": res["reason"],
        "message": res["message"],
        "raw_excerpt": res["raw_excerpt"],
        "attempts": res["attempts"],
        "exit_status": res.get("exit_status"),
        "parsed": smart.parse_smart(res["raw"],
                                    exit_status=res.get("exit_status")) if res["ok"] else None,
    }
    return out


# ---------------------------------------------------------------- preflight


class PreflightBody(BaseModel):
    path: str


@router.post("/collect/preflight")
def collect_preflight(body: PreflightBody) -> dict:
    """采集依赖检查：写权限 + smartctl 可用性 + 目标盘 SMART 能力。"""
    res: dict = {
        "path": body.path,
        "writable": False,
        "smartctl_available": False,
        "smartctl_path": None,
        "device_type_hint": "",
        "is_smart_capable": False,
        "warnings": [],
    }
    p = Path(body.path)
    if not p.is_dir():
        res["warnings"].append(f"路径不存在或不是目录：{body.path}")
        return res
    probe_file = p / ".cldm_preflight.tmp"
    try:
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        res["writable"] = True
    except OSError as exc:
        res["warnings"].append(f"目录不可写：{exc}")

    spath = smart.check_smartctl()
    res["smartctl_available"] = spath is not None
    res["smartctl_path"] = spath
    if spath is None:
        res["warnings"].append(
            "smartctl 不可用（安装 smartmontools，或设 CLDM_SMARTCTL 指定路径）")
        return res

    cands = smart.device_candidates_for_path(body.path)
    if not cands:
        res["warnings"].append("无法定位所在物理盘")
        return res

    r = smart.read_smart_verbose(devices=cands)
    if not r.get("ok"):
        msg = r.get("message") or ""
        res["warnings"].append(
            f"无法读取 SMART（{r.get('reason') or 'unknown'}）"
            + (f"：{msg}" if msg else "（盘不支持或需 root/管理员）"))
        return res
    parsed = smart.parse_smart(r["raw"], exit_status=r.get("exit_status"))
    res["device_type_hint"] = r["device_type"] or parsed.get("device_type") or ""
    res["is_smart_capable"] = parsed.get("health") in ("passed", "failed", "warning")
    if not res["is_smart_capable"]:
        res["warnings"].append("该盘未返回可用的 SMART 状态")
    return res
