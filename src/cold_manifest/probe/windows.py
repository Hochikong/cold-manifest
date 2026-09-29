"""Windows probe：PowerShell CIM/Storage cmdlet，单段 JSON 输出（§4.4）。

删除 v1 脆弱的 wmic 回退：命令失败直接 ProbeError。本模块在 WSL 下开发，
probe_path_win 依赖 powershell.exe；解析器与命令构造可用夹具单测覆盖。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import DiskInfo, ProbeError, VolumeInfo

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
    if manual_serial:
        info.disk_serial = manual_serial.strip()
        info.serial_source = "manual"
    return volume, info
