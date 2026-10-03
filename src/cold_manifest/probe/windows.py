"""Windows probe：PowerShell CIM/Storage cmdlet，单段 JSON 输出（§4.4）。

删除 v1 脆弱的 wmic 回退：命令失败直接 ProbeError。本模块在 WSL 下开发，
probe_path_win 依赖 powershell.exe；解析器与命令构造可用夹具单测覆盖。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import DiskInfo, ProbeError, VolumeInfo, VolumeTarget

# 单次调用产出一段 JSON：卷（Win32_LogicalDisk）+ 分区（Get-Partition）+ 盘
# （Get-Disk 的 PartitionStyle/BusType + Win32_DiskDrive 的桥型号/接口）。
PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$letter = '{letter}'
$l = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$${letter}:'"
if (-not $l) {{ throw "logical disk $${letter}: not found" }}
$p = Get-Partition -DriveLetter $letter
$dsk = Get-Disk -Number $p.DiskNumber
$dd = Get-CimInstance Win32_DiskDrive -Filter "Index=$($p.DiskNumber)"
[pscustomobject]@{{
  volume = @{{
    fs = $l.FileSystem
    label = $l.VolumeName
    vserial = $l.VolumeSerialNumber
    size = $l.Size
    free = $l.FreeSpace
  }}
  partition = @{{
    guid = $p.Guid
    index = $p.PartitionNumber
    offset = $p.Offset
    size = $p.Size
    mbrType = $p.MbrType
    gptType = $p.GptType
  }}
  disk = @{{
    index = $dd.Index
    model = $dd.Model
    serial = $dd.SerialNumber
    interface = $dd.InterfaceType
    firmware = $dd.FirmwareRevision
    size = $dd.Size
    friendlyName = $dsk.FriendlyName
    busType = $dsk.BusType
    partitionStyle = $dsk.PartitionStyle
  }}
}} | ConvertTo-Json -Depth 4
"""


def build_powershell_command(drive_letter: str) -> str:
    """构造 PowerShell 脚本文本（夹具单测断言用）。"""
    letter = drive_letter.rstrip(":").upper()
    if not (len(letter) == 1 and letter.isalpha()):
        raise ProbeError(f"非法盘符：{drive_letter!r}")
    return PS_SCRIPT.format(letter=letter)


def run_powershell(script: str, timeout: int = 60) -> str:
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ProbeError(f"powershell.exe 不可用：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError("PowerShell 探测超时") from exc
    if proc.returncode != 0:
        raise ProbeError(
            f"PowerShell 探测失败（rc={proc.returncode}）：{_decode(proc.stderr).strip()}"
        )
    return _decode(proc.stdout)


def _decode(raw: bytes) -> str:
    """powershell.exe 默认输出 OEM/ANSI 代码页（如 GBK），UTF-8 优先、GBK 兜底。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_windows_json(text: str, drive_letter: str) -> tuple[VolumeInfo, DiskInfo]:
    """解析 PowerShell 输出的 JSON。字段缺失/为 null 一律归空值，不抛异常；
    结构不合法才 ProbeError。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"PowerShell 输出不是合法 JSON：{exc}") from exc

    vol = data.get("volume") or {}
    part = data.get("partition") or {}
    disk = data.get("disk") or {}

    def s(obj: dict, key: str) -> str:
        v = obj.get(key)
        return str(v).strip() if v is not None else ""

    def i(obj: dict, key: str) -> int | None:
        v = obj.get(key)
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    vserial = s(vol, "vserial").upper()
    if vserial:
        vserial = vserial.zfill(8)  # Win32 VolumeSerialNumber 可能丢前导零

    partition_style = s(disk, "partitionStyle").upper()
    guid = s(part, "guid")
    partition_uuid = guid
    if not partition_uuid and partition_style == "MBR":
        # MBR 无 GUID：用磁盘签名不可得，留空，volume_id 生成层自行选择锚
        partition_uuid = ""

    volume = VolumeInfo(
        filesystem=s(vol, "fs"),
        label=s(vol, "label"),
        volume_serial_hex=vserial,
        partition_uuid=partition_uuid,
        partition_index=i(part, "index"),
        partition_table_type=partition_style or "unknown",
        capacity_bytes=i(vol, "size"),
        free_bytes=i(vol, "free"),
        mount_point=f"{drive_letter.rstrip(':').upper()}:\\",
        device_path=f"{drive_letter.rstrip(':').upper()}:",
    )

    smart_status = "unavailable"  # CIM 路径不提供 SMART；由调用方按需走 smartctl
    disk_serial = s(disk, "serial")
    serial_source = "probe" if disk_serial else ""
    info = DiskInfo(
        physical_model="",  # CIM 型号可能是 USB 桥，真盘型号留给 smartctl / 手动
        physical_serial="",
        disk_serial=disk_serial,
        serial_source=serial_source,
        bridge_model=s(disk, "model") or s(disk, "friendlyName"),
        interface_type=s(disk, "busType") or s(disk, "interface"),
        capacity_bytes=i(disk, "size"),
        firmware=s(disk, "firmware"),
        smart_status=smart_status,
    )
    return volume, info


# 多分区枚举（§4.2）：单次调用列出该盘全部带盘符分区；无盘符分区记入 skipped。
PS_ENUM_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$letter = '{letter}'
$p0 = Get-Partition -DriveLetter $letter
$dnum = $p0.DiskNumber
$skipped = @()
$vols = @()
foreach ($p in (Get-Partition -DiskNumber $dnum | Sort-Object PartitionNumber)) {{
  if (-not $p.DriveLetter) {{ $skipped += "partition#$($p.PartitionNumber)"; continue }}
  $ld = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($p.DriveLetter):'"
  if (-not $ld) {{ $skipped += "partition#$($p.PartitionNumber)"; continue }}
  $vols += [pscustomobject]@{{
    letter = $p.DriveLetter
    index = $p.PartitionNumber
    fs = $ld.FileSystem
    label = $ld.VolumeName
    size = $p.Size
  }}
}}
$dsk = Get-Disk -Number $dnum
[pscustomobject]@{{
  diskNumber = $dnum
  partitionStyle = $dsk.PartitionStyle
  volumes = $vols
  skipped = $skipped
}} | ConvertTo-Json -Depth 4
"""


def build_powershell_enum_command(drive_letter: str) -> str:
    """构造多分区枚举的 PowerShell 脚本文本（夹具单测断言用）。"""
    letter = drive_letter.rstrip(":").upper()
    if not (len(letter) == 1 and letter.isalpha()):
        raise ProbeError(f"非法盘符：{drive_letter!r}")
    return PS_ENUM_SCRIPT.format(letter=letter)


def parse_windows_volumes_json(text: str) -> "tuple[list[VolumeTarget], list[str]]":
    """解析枚举 JSON → (VolumeTarget 列表, warnings)。

    ConvertTo-Json 单元素时输出对象而非数组，此处统一处理；
    字段缺失归空值，不抛异常。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"PowerShell 输出不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ProbeError("PowerShell 枚举输出结构不合法")

    vols = data.get("volumes") or []
    if isinstance(vols, dict):  # 单卷时 ConvertTo-Json 不产数组
        vols = [vols]
    targets: list[VolumeTarget] = []
    for v in vols:
        if not isinstance(v, dict):
            continue
        letter = str(v.get("letter") or "").strip().rstrip(":").upper()
        if not letter:
            continue
        try:
            size = int(v["size"]) if v.get("size") is not None else None
        except (TypeError, ValueError):
            size = None
        try:
            index = int(v["index"]) if v.get("index") is not None else None
        except (TypeError, ValueError):
            index = None
        targets.append(
            VolumeTarget(
                path=f"{letter}:\\",
                device_path=f"{letter}:",
                partition_index=index,
                filesystem=str(v.get("fs") or "").strip(),
                label=str(v.get("label") or "").strip(),
                capacity_bytes=size,
                mount_point=f"{letter}:\\",
            )
        )
    warnings = [
        f"分区 {s} 无盘符或无法关联逻辑盘，跳过"
        for s in (data.get("skipped") or [])
        if isinstance(s, str)
    ]
    return targets, warnings


def enumerate_disk_volumes_win(path: str) -> "tuple[list[VolumeTarget], list[str]]":
    """枚举 path 所在物理盘的全部带盘符分区（Windows）。"""
    p = Path(path)
    if not p.exists():
        raise ProbeError(f"路径不存在：{path}")
    letter = str(p.resolve())[:2].rstrip(":")
    if not (len(letter) == 1 and letter.isalpha()):
        raise ProbeError(f"无法确定 {path} 的盘符（不支持 UNC/映射路径）")
    text = run_powershell(build_powershell_enum_command(letter))
    targets, warnings = parse_windows_volumes_json(text)
    if not targets:
        raise ProbeError(f"{path} 所在盘没有可采集的带盘符卷")
    return targets, warnings


def probe_path_win(
    path: str, *, manual_serial: str | None = None, smartctl: bool = True
) -> tuple[VolumeInfo, DiskInfo]:
    p = Path(path)
    if not p.exists():
        raise ProbeError(f"路径不存在：{path}")
    # 取盘符：'E:\foo' → 'E'；UNC 路径（\\server\share）不在冷备场景支持内
    letter = str(p.resolve())[:2].rstrip(":")
    if not (len(letter) == 1 and letter.isalpha()):
        raise ProbeError(f"无法确定 {path} 的盘符（不支持 UNC/映射路径）")

    text = run_powershell(build_powershell_command(letter))
    volume, info = parse_windows_json(text, letter)

    # smartctl 可选增强（P4-②）：拿不到不阻断。设备定位走身份解析
    # （PR-B）：按探测到的序列号/容量/型号在 smartctl 扫描表里校验身份，
    # 下标映射只作末位回退并标 risk；序列号覆盖改为有条件（见下）
    if smartctl:
        try:
            from ..smart import (parse_smart, read_smart_verbose,
                                 resolve_smart_device)

            data = json.loads(text)
            disk_raw = data.get("disk") or {}
            idx = disk_raw.get("index")
            probe_serial = info.disk_serial
            cands, si = resolve_smart_device(
                expected_serial=probe_serial or None,
                expected_capacity_bytes=info.capacity_bytes,
                expected_model=str(disk_raw.get("model") or "").strip() or None,
                disk_index=idx if isinstance(idx, int) else None,
                letter=letter)
            amb = si.get("identity_ambiguity")
            mapped_by = si.get("mapped_by")
            verified = mapped_by in ("serial_match", "env_override") and not amb
            info.identity_ambiguity = amb
            info.identity_verified = verified
            info.identity_risk = str(si.get("identity_risk")
                                     or (cands[0].get("risk") if cands else "")
                                     or "")
            warnings: list[str] = []
            if amb:
                warnings.append(f"设备身份歧义：{amb}")
            if si.get("identity_risk") == "target_not_found":
                # 已证明扫描表里没有目标盘：跳过 SMART 读取（宁可没有也不
                # 读错盘），保留 probe 序列号，原因落到 DiskInfo/scan_info
                info.identity_risk = "target_not_found"
                info.identity_reason = str(si.get("identity_reason") or "")
                warnings.append(f"已跳过 SMART 读取：{info.identity_reason}")
                info.identity_warnings = list(warnings)
                rsi = dict(si)
                rsi["identity_warnings"] = list(warnings)
                info.smart_scan_info = rsi
            else:
                res = read_smart_verbose(devices=cands)
                info.smart_attempts = res.get("attempts") or None
                rsi = dict(res.get("scan_info") or {})
                for k in ("mapped_by", "identity_risk", "identity_ambiguity"):
                    if si.get(k) is not None:
                        rsi[k] = si[k]
                if warnings:
                    rsi["identity_warnings"] = warnings
                info.smart_scan_info = rsi
                if res["ok"]:
                    info.smart_raw = res["raw"]
                    info.smart_device_type = res["device_type"]
                    parsed = parse_smart(res["raw"],
                                         exit_status=res.get("exit_status"))
                    info.physical_model = parsed.get("model") or info.physical_model
                    smart_serial = parsed.get("serial") or ""
                    if verified:
                        # 身份已验证（序列号精确匹配/环境覆盖且无歧义）：允许覆盖
                        if smart_serial:
                            info.physical_serial = smart_serial
                            if info.serial_source != "manual":
                                if probe_serial and smart_serial.strip().upper() \
                                        != probe_serial.strip().upper():
                                    warnings.append(
                                        f"SMART 序列号 {smart_serial} 与探测值"
                                        f" {probe_serial} 不一致，已按验证结果覆盖")
                                info.disk_serial = smart_serial
                                info.serial_source = "smartctl"
                    elif smart_serial:
                        # 未验证：只允许"补全"，否则保留 probe 值防错盘
                        info.physical_serial = smart_serial
                        if not probe_serial and info.serial_source != "manual":
                            info.disk_serial = smart_serial
                            info.serial_source = "smartctl_unverified"
                            warnings.append(
                                "探测序列号为空，采用未经验证的 smartctl 序列号"
                                "（serial_source=smartctl_unverified）")
                        else:
                            warnings.append(
                                "设备身份未验证（smartctl 读到的设备未能按序列号"
                                f"确认为目标盘，读到序列号 {smart_serial}），"
                                "为防错盘已保留探测值")
                    info.firmware = info.firmware or (parsed.get("firmware") or "")
                    if info.capacity_bytes is None and parsed.get("capacity_bytes"):
                        info.capacity_bytes = parsed["capacity_bytes"]
                    info.smart_status = parsed.get("health") or "unavailable"
                    info.smart_exit_status = res.get("exit_status")
                else:
                    # 拿不到不阻断，但留下可诊断原因（Windows 常见：需要管理员权限）
                    info.smart_error = res["message"]
                    info.smart_error_raw = res["raw_excerpt"]
                if warnings:
                    info.identity_warnings = list(warnings)
        except Exception:  # noqa: BLE001 — SMART 拿不到绝不阻断采集
            pass

    if manual_serial:
        info.disk_serial = manual_serial.strip()
        info.serial_source = "manual"
    return volume, info
