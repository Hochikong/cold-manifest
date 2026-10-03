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
from ..catalog import _usable_serial
from .state import get_state

router = APIRouter(prefix="/api", tags=["disks"])


# ---------------------------------------------------------------- helpers


def _latest_catalog_smart(conn: Any, disk_id: str) -> "dict | None":
    row = conn.execute(
        "SELECT * FROM disk_smart WHERE disk_id=? ORDER BY collected_at DESC LIMIT 1",
        (disk_id,),
    ).fetchone()
    return dict(row) if row else None


def _ssd_fields(item: Any) -> dict:
    """SMART 行（catalog 行 / meta 回填项）→ SSD 契约字段。

    catalog 行带 ssd_json 列；meta 回填项已带解析好的 ``ssd`` 子字典。
    """
    ssd = None
    if isinstance(item, dict):
        ssd = item.get("ssd")
        if not ssd:
            raw = item.get("ssd_json")
            if raw:
                try:
                    ssd = json.loads(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    ssd = None
    return smart.ssd_contract(ssd)


def _ata_fields(item: Any) -> dict:
    """SMART 行（catalog 行 / parse_smart 输出 / meta 回填项）→ ATA 契约字段
    （ata_attributes + 关键 HDD 指标 + 身份细节，缺项 null/[]/false）。"""
    return smart.ata_contract(item if isinstance(item, dict) else None)


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


@router.get("/identity-audit")
def identity_audit(request: Request) -> dict:
    """身份审计（PR-C，只读）：检测既有数据"串盘"迹象。

    同步执行；数据根快照多时可能数秒。返回
    ``{alerts, affected_disk_ids, summary{high,medium}, warnings}``。
    """
    from ..identity_audit import IdentityAuditError, audit_identities

    state = get_state(request)
    try:
        return audit_identities(state.data_root)
    except IdentityAuditError as e:
        raise HTTPException(status_code=400, detail=str(e)) from None


@router.get("/disks")
def list_disks(request: Request) -> dict:
    """盘列表：身份 + 最近 SMART 摘要 + 卷数/快照数 + 身份状态派生。"""
    from ..identity_audit import disk_identity_status

    state = get_state(request)
    rows = state.catalog.execute(
        """
        SELECT d.disk_id, d.physical_model, d.physical_serial, d.bridge_model,
               d.capacity_bytes, d.interface_type, d.first_seen, d.last_seen, d.nickname,
               d.identity_verified, d.identity_verified_source, d.identity_verified_at,
               (SELECT COUNT(*) FROM volumes v WHERE v.disk_id = d.disk_id) AS volume_count,
               (SELECT COUNT(*) FROM snapshots s JOIN volumes v2 ON s.volume_id = v2.volume_id
                WHERE v2.disk_id = d.disk_id) AS snapshot_count
        FROM disks d ORDER BY d.disk_id
        """
    ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        status, reason = disk_identity_status(
            state.catalog, state.data_root, r["disk_id"])
        item["identity_status"] = status
        item["identity_status_reason"] = reason
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


# Windows attached 枚举（两阶段）：PowerShell 只输出**原始事实**（Get-Disk /
# Get-Partition / Win32_DiskDrive / Win32_LogicalDisk 四个数组，各自 try/catch
# 容错），**关联在 Python 侧 _join_attached 做**（可测）。
#
# 为什么不直接在 PS 里 foreach Win32_DiskDrive 再 Get-Partition -DiskNumber
# $dd.Index：Win32_DiskDrive.Index（WMI 号）与 Get-Disk.Number（Storage 号）
# 不保证一致（NVMe/USB/RAID 混插实测见过错位），拿 WMI 号去查 Storage 的
# 分区会把卷挂错盘、把 A 盘的序列号配到 B 盘的卷上（真实事故：用户据此把
# 一块盘的采集登记到另一块盘名下）。Get-Disk.Number 是唯一主键。
_PS_ATTACHED = r"""
$ErrorActionPreference = 'Continue'
$res = [pscustomobject]@{
  disks = @(); partitions = @(); logicalDisks = @(); wmi = @(); warnings = @()
}
try {
  $res.disks = @(Get-Disk | Select-Object Number, SerialNumber, FriendlyName,
                 BusType, Size, @{n='OperationalStatus';e={($_.OperationalStatus) -join ','}})
} catch { $res.warnings += "Get-Disk: $($_.Exception.Message)" }
try {
  $res.partitions = @(Get-Partition -ErrorAction SilentlyContinue |
                      Select-Object DiskNumber, DriveLetter, PartitionNumber, Size)
} catch { $res.warnings += "Get-Partition: $($_.Exception.Message)" }
try {
  $res.logicalDisks = @(Get-CimInstance Win32_LogicalDisk |
                        Select-Object DeviceID, FileSystem, VolumeName)
} catch { $res.warnings += "Win32_LogicalDisk: $($_.Exception.Message)" }
try {
  $res.wmi = @(Get-CimInstance Win32_DiskDrive |
               Select-Object Index, Model, SerialNumber, Size, InterfaceType)
} catch { $res.warnings += "Win32_DiskDrive: $($_.Exception.Message)" }
$res | ConvertTo-Json -Depth 5
"""


def _as_list(x: Any) -> list:
    """ConvertTo-Json 单元素时输出对象而非数组；统一成列表。"""
    if isinstance(x, dict):
        return [x]
    return list(x or [])


def _to_int(v: Any) -> "int | None":
    try:
        return int(v) if v is not None and str(v) != "" else None
    except (TypeError, ValueError):
        return None


def _clean_serial(v: Any) -> str:
    s = str(v or "").strip()
    return "" if _is_placeholder_serial(s) else s


def _is_placeholder_serial(s: str) -> bool:
    """占位/哑序列号判定：全同字符（FFFFFF…）、纯 0、0123456789ABCDEF 型
    递增十六进制串等 USB 桥/虚拟盘常见的假序列号 → 不可信（视为空）。"""
    t = re.sub(r"[^0-9A-Za-z]", "", s or "").upper()
    if not t:
        return False  # 空串不是占位，是缺失
    if len(set(t)) == 1:
        return True
    hexseq = "0123456789ABCDEF"
    start = hexseq.find(t[0])
    if start >= 0:
        expect = "".join(hexseq[(start + k) % 16] for k in range(len(t)))
        if t == expect:
            return True
    return False


def _size_matches(a: int, b: int) -> bool:
    """同盘尺寸容差匹配：同一块盘两个 API 报的 Size 并不相等
    （4T: Get-Disk 4000787027968 vs Win32 4000784417280，差 ~2.6MB），
    相对差 ≤1% 且绝对差 ≤16MB 才认是同一块盘（取更严者）。"""
    diff = abs(a - b)
    return diff <= 16 * 1024 * 1024 and diff <= max(a, b) * 0.01


def _join_attached(disks: Any, parts: Any, wmi: Any,
                   logical_disks: Any = None) -> "list[dict]":
    """三源关联（可单测）：以 Get-Disk.Number 为唯一主键。

    - 卷：Get-Partition.DiskNumber == Number（同一 Storage 体系，可信）；
    - Win32_DiskDrive：必须 **Index 严格相等且 Size 容差匹配** 才认是同一块
      盘（真机证据：同盘两 API 报的 Size 差 ~2.6MB，Size 须用
      _size_matches 容差而非严格相等）；匹配不上绝不使用 WMI 的 model/serial
      （Index 单独相等不够——那正是本 bug 的根源）；WMI 缺失/匹配不上 →
      退化 Storage-only。
    - 序列号可信性：serial_source ∈ wmi_verified（WMI Index+Size 双匹配，优先
      —— Get-Disk 的序列号在部分 USB 桥上是盒子 ID 而非真盘序列号）|
      storage（WMI 不可得/未匹配，Get-Disk 自报）|
      ""；serial_verified 相应 true/false。拿不到可信序列号时 serial 留空，
      WMI 原始值放 serial_unverified_raw 仅供诊断（可能是别的盘的）。
    """
    ld_by_dev = {
        str(r.get("DeviceID") or "").upper(): r for r in _as_list(logical_disks)}
    items: "list[dict]" = []
    for d in _as_list(disks):
        num = _to_int(d.get("Number"))
        if num is None:
            continue
        vols = []
        for p in _as_list(parts):
            if _to_int(p.get("DiskNumber")) != num:
                continue
            letter = str(p.get("DriveLetter") or "").strip().rstrip(":").upper()
            if not letter:
                continue
            ldrow = ld_by_dev.get(f"{letter}:") or {}
            vols.append({
                "path": f"{letter}:\\",
                "drive_letter": letter,
                "filesystem": str(ldrow.get("FileSystem") or ""),
                "label": str(ldrow.get("VolumeName") or ""),
                "partition_number": _to_int(p.get("PartitionNumber")),
                "size_bytes": _to_int(p.get("Size")),
            })
        size = _to_int(d.get("Size"))
        wrow: "dict | None" = None
        wmi_index_hit: "dict | None" = None  # 仅 Index 命中（size 未知/不符）
        for w in _as_list(wmi):
            if _to_int(w.get("Index")) != num:
                continue
            wmi_index_hit = wmi_index_hit or w
            wsize = _to_int(w.get("Size"))
            if size and wsize and _size_matches(size, wsize):
                wrow = w
                break
        # 序列号优先级：WMI（Index+Size 双匹配通过）优先于存储层（Get-Disk）。
        # Get-Disk.SerialNumber 在部分 USB 桥上可能是盒子 ID 而非真盘序列号
        # （CrystalDiskInfo/ATA 直通读到的是后者），因此双匹配可信的 WMI 值
        # 优先；WMI 不可得/未通过双匹配再回退 Get-Disk 自报值。
        serial = _clean_serial((wrow or {}).get("SerialNumber"))
        if serial:
            serial_source, serial_verified = "wmi_verified", True
        else:
            serial = _clean_serial(d.get("SerialNumber"))
            serial_source, serial_verified = ("storage", True) if serial else ("", False)
        serial_unverified_raw = ""
        if not serial:
            raw = str((wmi_index_hit or {}).get("SerialNumber") or "").strip()
            if raw:
                serial_unverified_raw = raw
        model = str((wrow or {}).get("Model") or d.get("FriendlyName") or "").strip()
        items.append({
            "device": f"\\\\.\\PhysicalDrive{num}",
            "disk_number": num,
            "model": model,
            "serial": serial,
            "serial_source": serial_source,
            "serial_verified": serial_verified,
            "serial_unverified_raw": serial_unverified_raw,
            "system_serial": "",
            "wmi_matched": wrow is not None,
            "size_bytes": size,
            "bus_type": str(d.get("BusType") or ""),
            "operational_status": str(d.get("OperationalStatus") or ""),
            "interface_type": str((wrow or {}).get("InterfaceType") or ""),
            "volumes": vols,
        })
    return items


def _smartctl_scan_entries() -> "list[dict]":
    """smartctl --scan-open -j 一次（回退 --scan -j）。

    返回 devices 条目列表（name/type/…）；smartctl 缺失/失败 → []。
    空结果不缓存失败的形态（每次调用现扫，量级 ≤20 盘可接受）。"""
    import subprocess

    from ..probe.windows import _decode
    from ..smart import smartctl_exec

    exe = smartctl_exec()
    if not exe:
        return []
    for args in (["--scan-open", "-j"], ["--scan", "-j"]):
        try:
            proc = subprocess.run([exe, *args],
                                  capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if not proc.stdout.strip():
            continue
        try:
            data = json.loads(_decode(proc.stdout))
        except (ValueError, UnicodeDecodeError):
            continue
        devs = [d for d in _as_list(data.get("devices"))
                if str(d.get("name") or "").strip()]
        if devs:
            return devs
    return []


def _smartctl_device_for(item: dict, entries: "list[dict]") -> "dict | None":
    """每盘映射 smartctl 设备：scan-open 下标优先（下标 == 盘号），其次
    /dev/pdN 名字直配（Windows PhysicalDriveN）。每盘只挑一个候选。"""
    num = item.get("disk_number")
    if num is None:
        return None
    for e in entries:
        if str(e.get("name") or "").rstrip("/").endswith(f"pd{num}"):
            return e
    if 0 <= num < len(entries):
        return entries[num]
    return None


def _smartctl_read_identity(device: str, dtype: str,
                            expect_size: int) -> "dict | None":
    """smartctl <dev> [-d <type>] -i -j 读一次：取 ATA 真序列号/型号。

    容量容差复核（同盘两个 API 报的 Size 本就不等，用 _size_matches），
    容量不符 → 不是这块盘，返回 None。exit_status 低 2 位有致命错误
    （命令行错/打不开设备，如非管理员）→ None。"""
    import subprocess

    from ..probe.windows import _decode
    from ..smart import smartctl_exec

    exe = smartctl_exec()
    if not exe:
        return None
    cmd = [exe, device]
    t = str(dtype or "").strip()
    if t and t != "auto":
        cmd += ["-d", t]
    cmd += ["-i", "-j"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode & 0x3:  # bit0 命令行错 / bit1 设备打不开
        return None
    try:
        data = json.loads(_decode(proc.stdout))
    except (ValueError, UnicodeDecodeError):
        return None
    serial = _clean_serial(data.get("serial_number"))
    if not serial:
        return None
    cap = data.get("user_capacity") or {}
    cap_bytes = cap.get("bytes") if isinstance(cap, dict) else None
    if expect_size and cap_bytes:
        try:
            if not _size_matches(int(expect_size), int(cap_bytes)):
                return None
        except (TypeError, ValueError):
            return None
    model = str(data.get("model_name") or data.get("model_family")
                or "").strip()
    return {"serial": serial, "model": model,
            "device": device, "device_type": t}


def _smartctl_attach_serials(items: "list[dict]",
                             warnings: "list[str]") -> None:
    """就地增强：每盘优先用**卷盘符**直接 smartctl -i -j <letter>: 读一次
    （真机验证：smartctl 接受盘符，返回精确型号 + ATA 真序列号，与
    /dev/sdX 读数一致），每盘一个盘符内走类型兜底链（"" → sat）。

    盘符打不开/没有盘符/读数不符 → 退回 scan-open 映射 + 容量复核。
    容量只用于**排除明显不符**，绝不作为认同依据（认同靠盘符/下标）。

    - serial 优先 ATA 值（serial_source="smartctl"、serial_verified=True），
      否则保留系统枚举值（Get-Disk/WMI——USB 盒上可能是盒子 ID），
      但 serial_verified=False（非 ATA 直通都算未验证）；
    - system_serial：serial 与系统值不同时保留系统值（前端标注
      "系统枚举 ID，可能是盒子 ID"）；
    - smartctl 全线不可得 → 整体回退，警告一次，不加噪声。"""

    def _apply(it: dict, ident: "dict | None") -> bool:
        if ident is None:
            if it.get("serial"):
                it["serial_verified"] = False
            return False
        sys_serial = str(it.get("serial") or "")
        ata = ident["serial"]
        it["serial"] = ata
        it["serial_source"] = "smartctl"
        it["serial_verified"] = True
        it["system_serial"] = "" if sys_serial == ata else sys_serial
        if ident.get("model"):
            it["model"] = ident["model"]
        if it.get("serial_unverified_raw") == ata:
            it["serial_unverified_raw"] = ""
        return True

    def _letter_reads(it: dict) -> "dict | None":
        size = int(it.get("size_bytes") or 0)
        for v in it.get("volumes") or []:
            letter = str(v.get("drive_letter") or "").strip()
            if not letter:
                continue
            dev = f"{letter}:"
            for dtype in ("", "sat"):  # 类型兜底链：裸盘符 → -d sat
                try:
                    ident = _smartctl_read_identity(dev, dtype, size)
                except Exception:  # noqa: BLE001
                    ident = None
                if ident is not None:
                    return ident
        return None

    def _scan_read(it: dict, entries: "list[dict]") -> "dict | None":
        entry = _smartctl_device_for(it, entries)
        if entry is None:
            return None
        try:
            return _smartctl_read_identity(
                str(entry.get("name")), str(entry.get("type") or ""),
                int(it.get("size_bytes") or 0))
        except Exception:  # noqa: BLE001
            return None

    try:
        entries = _smartctl_scan_entries()
    except Exception:  # noqa: BLE001 — 枚举绝不能拖垮快选
        entries = []
    got_any = False
    for it in items:
        if _apply(it, _letter_reads(it)):
            got_any = True
            continue
        if entries and _apply(it, _scan_read(it, entries)):
            got_any = True
    if not got_any:
        for it in items:
            if it.get("serial"):
                it["serial_verified"] = False
        warnings.append("smartctl 不可用（未安装或无管理员权限），"
                        "序列号为系统枚举值，可能是盒子 ID")


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
    warnings = [str(w) for w in _as_list(data.get("warnings"))]
    disks = _as_list(data.get("disks"))
    if not disks:
        reason = "；".join(warnings) or "Get-Disk 未返回任何磁盘"
        return {"available": False, "reason": reason, "items": [],
                "warnings": warnings}
    items = _join_attached(disks, data.get("partitions"), data.get("wmi"),
                           data.get("logicalDisks"))
    _smartctl_attach_serials(items, warnings)
    return {"available": True, "items": items, "count": len(items),
            "warnings": warnings}


@router.get("/disks/{disk_id}")
def disk_detail(disk_id: str, request: Request) -> dict:
    """盘详情：身份 + 卷列表 + 最近 SMART（catalog 优先，缺则 meta 回填）+ 快照时间线。"""
    state = get_state(request)
    row = state.catalog.execute("SELECT * FROM disks WHERE disk_id=?", (disk_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"盘不存在：{disk_id}")
    detail = dict(row)
    from ..identity_audit import disk_identity_status

    status, reason = disk_identity_status(state.catalog, state.data_root, disk_id)
    detail["identity_status"] = status
    detail["identity_status_reason"] = reason
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
    # SSD/ATA 契约同时挂在顶层与 latest_smart 内层：前端健康卡读的是
    # latest_smart.*（与 /smart 历史序列每项的形状保持一致），顶层那份保留
    # 给其它调用方——两层必须一致（ssd 只挂顶层导致前端读不到的前车之鉴）。
    ssd = _ssd_fields(latest) if latest is not None else None
    ata = _ata_fields(latest) if latest is not None else {
        "ata_attributes": [], **{k: (False if k == "trim" else None)
                                 for k in smart.ATA_CONTRACT_KEYS}}
    if latest is not None:
        latest["ssd"] = ssd
        latest.update(ata)
    detail["latest_smart"] = latest
    detail["ssd"] = ssd
    detail.update(ata)
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
        r["ssd"] = _ssd_fields(r)
        r.update(_ata_fields(r))
        items.append(r)
    for item in _meta_backfill(state, disk_id):
        item["ssd"] = _ssd_fields(item)
        item.update(_ata_fields(item))
        items.append(item)
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


def _verify_target(attached: dict, expected_serial: str,
                   path: "str | None") -> "tuple[str, dict | None, str]":
    """读取前核验目标盘：返回 (status, 命中项, 当前序列号)。

    status ∈ ok / not_attached / identity_mismatch / serial_unverified。
    在本机全量枚举结果（复用 /api/disks/attached 同一链路，Windows 侧含
    smartctl ATA 真序列号增强）里找目标盘：

    - 有 path：按卷路径定位（挂载点唯一，不依赖序列号）；
    - 无 path：按期望序列号（catalog physical_serial，缺省 disk_id）定位；
    - 定位不到 → not_attached（盘不在线）；
    - 定位到但真序列号不可得（serial_verified 显式 False，或序列号为空）
      → serial_unverified（宁可不读，也不冒险把别的盘当目标盘）；
    - 有 path 时定位到的盘真序列号与期望不一致 → identity_mismatch
      （同盒换盘 / 插成同型号另一块）。无 path 时按序列号定位命中即一致。

    注：serial_verified 缺省（Linux lsblk 枚举）视为已取得真序列号——
    lsblk SERIAL 是盘体硬件序列号；Windows 枚举恒带该键。
    """
    items = attached.get("items") or []
    want = str(path or "").rstrip("\\/").lower()
    item = None
    if want:
        item = next(
            (i for i in items if any(
                str(v.get("path") or "").rstrip("\\/").lower() == want
                for v in i.get("volumes") or [])),
            None,
        )
    else:
        exp = expected_serial.strip().upper()
        item = next(
            (i for i in items
             if str(i.get("serial") or "").strip().upper() == exp),
            None)
    if item is None:
        return "not_attached", None, ""
    cur = str(item.get("serial") or "").strip()
    if not cur or item.get("serial_verified") is False:
        return "serial_unverified", item, cur
    if want and cur.strip().upper() != expected_serial.strip().upper():
        return "identity_mismatch", item, cur
    return "ok", item, cur


_VERIFY_MESSAGES = {
    "not_attached":
        "目标盘当前不在线（本机未枚举到序列号为 {expected} 的磁盘），"
        "未读取 SMART。请插上该盘后重试。",
    "identity_mismatch":
        "读取被拒绝：当前定位到的盘序列号为 {current}，与记录的期望序列号 "
        "{expected} 不一致（可能同盒换了盘，或插成了同型号的另一块盘），"
        "未读取 SMART。请核对磁盘后重试。",
    "serial_unverified":
        "目标盘在线但无法取得可信的真序列号（当前枚举值：{current}），"
        "为避免读错盘未读取 SMART。请手动核对盘序列号后重试。",
    "enumeration_failed":
        "本机磁盘枚举失败，无法核验目标盘是否在线，未读取 SMART。"
        "请检查系统磁盘枚举后重试。",
}


def _verify_denied(disk_id: str, reason: str, expected_serial: str,
                   current_serial: str, extra: str = "") -> dict:
    """核验拒绝的统一响应体（200 + ok=false + 人话原因 + 期望/当前序列号）。"""
    msg = _VERIFY_MESSAGES[reason].format(
        expected=expected_serial or "（空）",
        current=current_serial or "（空）") + extra
    return {
        "disk_id": disk_id,
        "device": None,
        "device_candidates": None,
        "scan_info": None,
        "ok": False,
        "device_type": "",
        "reason": reason,
        "message": msg,
        "raw_excerpt": "",
        "attempts": [],
        "exit_status": None,
        "parsed": None,
        "ssd": None,
        "expected_serial": expected_serial or None,
        "current_serial": current_serial or None,
    }


def _locate_attached_candidates(disk_id: str, path: "str | None",
                                attached: "dict | None" = None,
                                serial_hint: "str | None" = None
                                ) -> "tuple[Any, list[dict], dict]":
    """定位当前插着的盘的**候选链**：返回 (attached 容量或 None, 候选列表, 定位信息)。

    attached：调用方已枚举好的本机盘列表（如 smart/read 读取前的核验步骤），
    传入则不再重复枚举。serial_hint：期望序列号（catalog physical_serial），
    无 path 定位时优先于 disk_id 参与匹配。

    定位信息 loc = {expected_serial?, scan_info?}：能拿到目标盘序列号时走
    resolve_smart_device 按身份（序列号优先）定位，scan_info 透传给调用方
    展示映射结论（mapped_by/identity_ambiguity）。

    现场读取必须走候选链，单设备曾导致 USB 桥盘
    永远打不开（PhysicalDrive 形态在桥上常 Invalid argument）。有 path：
    Windows 优先在 attached 枚举里反查盘号走全链，反查不到退回
    smart.device_candidates_for_path；Linux 同样先在 attached 枚举里
    反查（带序列号/容量走 resolve_smart_device 身份定位），反查不到
    退回 smart.device_candidates_for_path（/proc/mounts 定位）。
    无 path：按序列号在 attached 里找。定位不到抛 HTTPException 404。
    """
    loc: dict = {}
    attached = attached if attached is not None else None
    if attached is None:
        need_attached = path is None or sys.platform == "win32" \
            or sys.platform.startswith("linux")
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
            exp_serial = str(item.get("serial") or "").strip() or None
            if exp_serial or idx is not None:
                cands, si = smart.resolve_smart_device(
                    expected_serial=exp_serial,
                    expected_capacity_bytes=item.get("size_bytes"),
                    expected_model=str(item.get("model") or "").strip() or None,
                    disk_index=idx)
                loc = {"expected_serial": exp_serial, "scan_info": si}
            else:
                cands = smart.device_candidates_for_path(path)
            return item.get("size_bytes"), cands, loc
        cands = smart.device_candidates_for_path(path)
        if not cands:
            raise HTTPException(status_code=404,
                                detail=f"无法定位 {path} 所在物理盘")
        return None, cands, loc

    want = (serial_hint or disk_id or "").strip()
    item = next(
        (i for i in (attached or {}).get("items") or []
         if str(i.get("serial") or "").strip() == want),
        None,
    )
    if item is None:
        raise HTTPException(status_code=404,
                            detail=f"盘 {disk_id} 当前不在线（未在 /api/disks/attached 中）")
    idx = _disk_index_of(item)
    cands, si = smart.resolve_smart_device(
        expected_serial=want or None,
        expected_capacity_bytes=item.get("size_bytes"),
        expected_model=str(item.get("model") or "").strip() or None,
        disk_index=idx)
    loc = {"expected_serial": want or None, "scan_info": si}
    if not cands:
        # 扫描表空且无盘号回退形态：退回 attached 枚举的设备串/卷路径
        dev = str(item.get("device") or "").strip()
        if dev:
            cands = [{"device": dev, "type": "", "source": "attached"}]
        elif item.get("volumes"):
            cands = smart.device_candidates_for_path(
                item["volumes"][0].get("path") or "")
    if not cands:
        raise HTTPException(status_code=404,
                            detail=f"盘 {disk_id} 在线但无法定位物理设备")
    return item.get("size_bytes"), cands, loc


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

    **读取前先核验目标盘**：先枚举本机全部磁盘（复用 /api/disks/attached
    同一链路，Windows 侧含 smartctl ATA 真序列号增强），确认目标盘已插上
    且真序列号与记录一致（catalog physical_serial，缺省 disk_id），才执行
    读取；否则一律不读，返回 200 + ok=false + 人话原因，绝不 500。

    reason 取值（供前端展示）：
    - None：核验通过并完成读取（读取本身失败时为 smart.read_verbose 的原因）；
    - not_attached：本机未枚举到该盘（不在线）；
    - identity_mismatch：定位到的盘真序列号与记录不一致（列出记录/当前）；
    - serial_unverified：目标盘在线但拿不到可信真序列号（宁可不读）；
    - enumeration_failed：本机磁盘枚举失败，无法核验；
    - 其余（permission_denied / device_open / timeout / …）：核验通过后的
      读取阶段失败，沿用 smart.read_verbose 的原因集。

    响应体额外带 expected_serial / current_serial（期望与核验时的当前序列号），
    便于前端展示与排障。
    """
    state = get_state(request)
    path = body.path if body else None
    row = state.catalog.execute(
        "SELECT physical_serial FROM disks WHERE disk_id=?", (disk_id,)).fetchone()
    expected_serial = (str(row[0]).strip() if row and row[0] else "") or disk_id

    # ① 枚举本机全部磁盘（与快选同一链路）
    try:
        if sys.platform.startswith("linux"):
            attached = _attached_linux()
        elif sys.platform == "win32":
            attached = _attached_win()
        else:
            attached = None
    except Exception as e:  # noqa: BLE001 — 枚举失败绝不静默读取
        attached = {"available": False, "reason": str(e), "items": []}
    if not attached or not attached.get("available"):
        detail = str((attached or {}).get("reason") or "枚举不可用")
        return _verify_denied(disk_id, "enumeration_failed",
                              expected_serial, "", extra=f"（{detail}）")

    # ② 核验目标盘在线 + 身份一致
    status, _item, cur = _verify_target(attached, expected_serial, path)
    if status != "ok":
        return _verify_denied(disk_id, status, expected_serial, cur)

    # ③ 核验通过 → 既有读取逻辑（候选链）
    size_bytes, cands, loc = _locate_attached_candidates(
        disk_id, path, attached=attached, serial_hint=expected_serial)
    size_bytes, cap_status = _capacity_screen(cands, size_bytes)
    if not cands:
        raise HTTPException(status_code=404, detail="无法定位该盘的物理设备候选")
    res = smart.read_smart_verbose(devices=cands)
    if cap_status:
        res.setdefault("scan_info", {})["capacity_check"] = cap_status
    loc_si = loc.get("scan_info")
    if loc_si:
        merged_si = dict(res.get("scan_info") or {})
        for k in ("mapped_by", "identity_risk", "identity_ambiguity"):
            if loc_si.get(k) is not None:
                merged_si[k] = loc_si[k]
        res["scan_info"] = merged_si
    device = res.get("device") or (cands[0]["device"] if cands else "")
    parsed = smart.parse_smart(res["raw"],
                               exit_status=res.get("exit_status")) if res["ok"] else None
    # 身份核验（PR-B）：读到序列号与目标盘期望序列号不一致 → ok=false，
    # 防止把另一块盘的 SMART 当成目标盘的数据返回
    reason = res["reason"]
    message = res["message"]
    expected_serial = str(loc.get("expected_serial") or "").strip()
    loc_si = loc.get("scan_info") or {}
    if parsed and expected_serial and loc_si.get("mapped_by"):
        # 仅当定位真走过身份解析（scan_info 带 mapped_by）时才核验，
        # 避免退化路径（attached 单候选等）误伤
        got = str(parsed.get("serial") or "").strip()
        if got and got.upper() != expected_serial.upper():
            res["ok"] = False
            reason = "identity_mismatch"
            message = (
                f"读到的是另一块盘：smartctl 报告序列号 {got}，"
                f"与目标盘期望序列号 {expected_serial} 不一致"
                "（同型号同容量多盘时映射可能错位）；"
                "建议拔掉其它同型号盘再试，或用 --serial 显式区分")
    out: dict = {
        "disk_id": disk_id,
        "device": device,
        "device_candidates": res.get("device_candidates"),
        "scan_info": res.get("scan_info"),
        "ok": res["ok"],
        "device_type": res["device_type"],
        "reason": reason,
        "message": message,
        "raw_excerpt": res["raw_excerpt"],
        "attempts": res["attempts"],
        "exit_status": res.get("exit_status"),
        "parsed": parsed,
        "ssd": _ssd_fields(parsed) if parsed else None,
        "expected_serial": expected_serial or None,
        "current_serial": cur or None,
    }
    return out


# ---------------------------------------------------------------- 身份现场重新校验


@router.post("/disks/{disk_id}/identity/recheck")
def disk_identity_recheck(disk_id: str, request: Request) -> dict:
    """现场重新校验这块盘的身份（把插着的盘与登记身份对号）。

    定位复用 /smart/read 的候选链逻辑（attached 枚举 → resolve_smart_device
    身份定位 → read_smart_verbose）；判据与采集 probe 相同（resolve_smart_device
    的 mapped_by：serial_match / unique_capacity_match 视为验证通过）。

    - 盘不在线：200 + {"ok": false, "verdict": "not_attached", ...}（未知盘 404）；
    - 验证通过：identity_verified=1、source='auto'、at=now，读到不同
      physical_serial 时顺带更正记录值；
    - 未通过：identity_verified=0（保留原因在响应里）。

    本端点**绝不**改 disk_id / volume_id（会破坏快照关联）；只更正/确认
    记录性身份字段。返回 {ok, verdict, reason, identity_serial, capacity_bytes}。
    """
    from datetime import datetime, timezone

    state = get_state(request)
    if state.catalog.execute(
            "SELECT 1 FROM disks WHERE disk_id=?", (disk_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail=f"盘不存在：{disk_id}")

    def _body(ok: bool, verdict: str, reason: str,
              identity_serial: "str | None", capacity_bytes: "int | None") -> dict:
        return {"ok": ok, "verdict": verdict, "reason": reason,
                "identity_serial": identity_serial,
                "capacity_bytes": capacity_bytes}

    try:
        size_bytes, cands, loc = _locate_attached_candidates(disk_id, None)
    except HTTPException as e:
        # attached 枚举里没有这块盘（含枚举失败）→ 盘不在线，不是错误
        return _body(False, "not_attached", str(e.detail), None, None)

    size_bytes, _cap_status = _capacity_screen(cands, size_bytes)
    if not cands:
        return _body(False, "unverified", "盘在线但无法定位物理设备候选",
                     None, size_bytes)

    res = smart.read_smart_verbose(devices=cands)
    if not res.get("ok"):
        state.catalog.execute(
            "UPDATE disks SET identity_verified=0, identity_verified_source='auto',"
            " identity_verified_at=? WHERE disk_id=?",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), disk_id))
        state.catalog.commit()
        reason = res.get("message") or res.get("reason") or "SMART 读取失败"
        return _body(False, "unverified", f"无法读取 SMART：{reason}",
                     None, size_bytes)

    parsed = smart.parse_smart(res["raw"], exit_status=res.get("exit_status"))
    got = str(parsed.get("serial") or "").strip()
    expected = str(loc.get("expected_serial") or "").strip() or disk_id
    mapped_by = str((loc.get("scan_info") or {}).get("mapped_by") or "")
    capacity = parsed.get("capacity_bytes") or size_bytes

    # 与采集 probe 相同的判据：序列号一致，或身份解析走到
    # unique_capacity_match（expected 是盒 ID、SMART 读到真盘序列号且容量吻合）
    verified = bool(got) and (
        got.upper() == expected.upper() or mapped_by == "unique_capacity_match")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if verified:
        sets = ["identity_verified=1", "identity_verified_source='auto'",
                "identity_verified_at=?"]
        params: "list[object]" = [now]
        if got and got.upper() != str(
                state.catalog.execute(
                    "SELECT physical_serial FROM disks WHERE disk_id=?",
                    (disk_id,)).fetchone()[0] or "").strip().upper():
            sets.append("physical_serial=?")
            params.append(got)
        params.append(disk_id)
        state.catalog.execute(
            f"UPDATE disks SET {', '.join(sets)} WHERE disk_id=?", params)
        reason = ("现场读取 SMART：序列号一致" if got.upper() == expected.upper()
                  else "现场读取 SMART：盒 ID + 容量/真盘序列号吻合（unique_capacity_match）")
    else:
        state.catalog.execute(
            "UPDATE disks SET identity_verified=0, identity_verified_source='auto',"
            " identity_verified_at=? WHERE disk_id=?", (now, disk_id))
        if got:
            reason = (f"现场读到序列号 {got}，与登记序列号 {expected} 不一致"
                      "（可能插错盘或映射错位；请核对后重试）")
        else:
            reason = "现场未能读到序列号，无法确认身份"
    state.catalog.commit()
    return _body(verified, "verified" if verified else "unverified",
                 reason, got or None, capacity)


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
        "requires_manual_serial": False,
        "manual_serial_reason": "",
        "probe_serial": None,
        "smart_serial": None,
        "serial_usable": False,
        "bridge_model": None,
        "interface_type": None,
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

    # 序列号手填判定：复用采集引擎同一判据（requires_manual_serial）——
    # 只有 ATA 直通与系统枚举序列号都不可用时才要求手填；USB 盒与否仅影响文案。
    from ..collect import requires_manual_serial, serial_required_message
    from ..probe import ProbeError, probe_path

    probe_serial = ""
    smart_serial = ""
    try:
        # 与 collect_volume 完全同参：smartctl=True 才会填 physical_serial
        # （ATA 直通真盘序列号）；关掉它 preflight 就少了 collect 有的一路取数。
        _, disk = probe_path(body.path, smartctl=True)
    except ProbeError:
        disk = None  # 探测失败沿用现有错误路径，不新增报错
        # probe 未跑成时真盘序列号仍取上面 read_smart_verbose 的解析结果
        smart_serial = str(parsed.get("serial") or "").strip()
    if disk is not None:
        # probe_serial = 系统枚举（Windows/Get-Disk 层）上报的 ID，可能是盒子/桥的；
        # smart_serial = smartctl ATA 直通读到的真盘序列号（probe 未跑 SMART 时
        # 用上面 read_smart_verbose 的解析结果兜底，两者本应一致）。
        probe_serial = str(getattr(disk, "disk_serial", "") or "").strip()
        smart_serial = (str(getattr(disk, "physical_serial", "") or "").strip()
                        or str(parsed.get("serial") or "").strip())
        res["bridge_model"] = str(disk.bridge_model or "").strip() or None
        res["interface_type"] = str(disk.interface_type or "").strip() or None
        if requires_manual_serial(disk):
            res["requires_manual_serial"] = True
            res["manual_serial_reason"] = serial_required_message(disk, None, None)
    res["probe_serial"] = probe_serial or None
    res["smart_serial"] = smart_serial or None
    res["serial_usable"] = _usable_serial(probe_serial) or _usable_serial(smart_serial)
    return res
