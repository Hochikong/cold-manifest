"""磁盘元数据采集：用户提供序列号，其他元数据通过 PowerShell/WMI 获取。"""

import json
import logging
import os
import subprocess

from backuptools.utils import sanitize_name

logger = logging.getLogger(__name__)

# PowerShell 脚本：盘符 → 逻辑卷 → 分区 → 物理磁盘
_PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$drive = '{drive}'

$getInstance = $null
try {{
    Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='$drive'" -ErrorAction Stop | Out-Null
    $getInstance = 'CIM'
}} catch {{
    $getInstance = 'WMI'
}}

if ($getInstance -eq 'CIM') {{
    $logical = Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='$drive'"
    $partition = Get-CimInstance -Query "ASSOCIATORS OF {{Win32_LogicalDisk.DeviceID='$drive'}} WHERE ResultClass=Win32_DiskPartition"
    if (-not $partition) {{
        $result = @{{
            LogicalDisk = @{{
                DeviceID = $logical.DeviceID
                VolumeName = $logical.VolumeName
                FileSystem = $logical.FileSystem
                FreeSpace = $logical.FreeSpace
                Size = $logical.Size
            }}
            Partition = $null
            PhysicalDisk = $null
        }}
        $result | ConvertTo-Json -Depth 4 -Compress
        exit 0
    }}
    $physical = Get-CimInstance -Query "ASSOCIATORS OF {{Win32_DiskPartition.DeviceID='$($partition.DeviceID -replace '\\','\\')'}} WHERE ResultClass=Win32_DiskDrive"
    $result = @{{
        LogicalDisk = @{{
            DeviceID = $logical.DeviceID
            VolumeName = $logical.VolumeName
            FileSystem = $logical.FileSystem
            FreeSpace = $logical.FreeSpace
            Size = $logical.Size
        }}
        Partition = @{{
            Index = $partition.Index
            Size = $partition.Size
        }}
        PhysicalDisk = @{{
            SerialNumber = $physical.SerialNumber
            Model = $physical.Model
            Size = $physical.Size
            FirmwareRevision = $physical.FirmwareRevision
            InterfaceType = $physical.InterfaceType
        }}
    }}
}} else {{
    $logical = Get-WmiObject -Class Win32_LogicalDisk -Filter "DeviceID='$drive'"
    $partition = Get-WmiObject -Query "ASSOCIATORS OF {{Win32_LogicalDisk.DeviceID='$drive'}} WHERE ResultClass=Win32_DiskPartition"
    if (-not $partition) {{
        $result = @{{
            LogicalDisk = @{{
                DeviceID = $logical.DeviceID
                VolumeName = $logical.VolumeName
                FileSystem = $logical.FileSystem
                FreeSpace = $logical.FreeSpace
                Size = $logical.Size
            }}
            Partition = $null
            PhysicalDisk = $null
        }}
        $result | ConvertTo-Json -Depth 4 -Compress
        exit 0
    }}
    $physical = Get-WmiObject -Query "ASSOCIATORS OF {{Win32_DiskPartition.DeviceID='$($partition.DeviceID -replace '\\','\\')'}} WHERE ResultClass=Win32_DiskDrive"
    $result = @{{
        LogicalDisk = @{{
            DeviceID = $logical.DeviceID
            VolumeName = $logical.VolumeName
            FileSystem = $logical.FileSystem
            FreeSpace = $logical.FreeSpace
            Size = $logical.Size
        }}
        Partition = @{{
            Index = $partition.Index
            Size = $partition.Size
        }}
        PhysicalDisk = @{{
            SerialNumber = $physical.SerialNumber
            Model = $physical.Model
            Size = $physical.Size
            FirmwareRevision = $physical.FirmwareRevision
            InterfaceType = $physical.InterfaceType
        }}
    }}
}}
$result | ConvertTo-Json -Depth 4 -Compress
"""


def _run_powershell(drive: str) -> dict:
    script = _PS_SCRIPT.format(drive=drive)
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-Command", script,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if stderr:
            raise RuntimeError(f"PowerShell failed: {stderr}")
        raise RuntimeError(f"PowerShell exited with code {proc.returncode}")
    output = proc.stdout.strip()
    if not output:
        raise RuntimeError("PowerShell returned empty output")
    return json.loads(output)


def _run_wmic(drive: str) -> dict:
    result = {"LogicalDisk": {}, "Partition": None, "PhysicalDisk": None}
    ld_cmd = [
        "wmic", "logicaldisk",
        "where", f"DeviceID='{drive}'",
        "get", "DeviceID,VolumeName,FileSystem,FreeSpace,Size",
        "/format:csv",
    ]
    proc = subprocess.run(ld_cmd, capture_output=True, text=True, timeout=15)
    if proc.returncode == 0:
        lines = [l.strip() for l in proc.stdout.strip().splitlines() if l.strip()]
        if len(lines) >= 2:
            header = lines[0].split(",")
            data = lines[1].split(",")
            for i, h in enumerate(header):
                if i < len(data):
                    result["LogicalDisk"][h.strip()] = data[i].strip()
    logger.warning("wmic fallback: partition and physical disk info not available")
    return result


def _generate_volume_id(serial: str, partition_index: int) -> str:
    if serial and serial.strip():
        base = sanitize_name(serial.strip())
    else:
        base = "NO_SERIAL"
    if partition_index is None:
        partition_index = 0
    return f"{base}_P{partition_index}"


def _int_or_none(val):
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def collect_metadata(drive: str, manual_serial: str = None) -> dict:
    """采集磁盘元数据。

    如果提供 manual_serial，直接使用作为 disk_serial 和 volume_id 的基础。
    否则从 WMI 获取（对 NVMe 盘可能不准确）。
    """
    drive = drive.rstrip(":\\") + ":"
    if not os.path.exists(drive):
        raise FileNotFoundError(f"Drive not found: {drive}")

    raw = None
    try:
        raw = _run_powershell(drive)
    except Exception as e:
        logger.warning("PowerShell failed (%s), falling back to wmic", e)
        try:
            raw = _run_wmic(drive)
        except Exception as e2:
            raise RuntimeError(
                f"Both PowerShell and wmic failed for {drive}"
            ) from e2

    ld = raw.get("LogicalDisk", {}) or {}
    part = raw.get("Partition") or {}
    phys = raw.get("PhysicalDisk") or {}

    part_idx = _int_or_none(part.get("Index"))

    if manual_serial:
        serial = manual_serial.strip()
        logger.info("Using manual serial: %s", serial)
    else:
        serial = (phys.get("SerialNumber") or "").strip()
        logger.info("Using WMI serial: %s", serial)

    volume_id = _generate_volume_id(serial, part_idx)

    meta = {}

    def add(key, value):
        meta[key] = str(value) if value is not None else ""

    add("volume_id", volume_id)
    add("disk_serial", serial)
    add("serial_source", "manual" if manual_serial else "wmi")
    add("partition_index", part_idx if part_idx is not None else "")
    add("model", (phys.get("Model") or "").strip())
    add("disk_capacity_bytes", phys.get("Size"))
    add("partition_capacity_bytes", part.get("Size"))
    add("filesystem", ld.get("FileSystem"))
    add("volume_label", ld.get("VolumeName"))
    add("firmware", (phys.get("FirmwareRevision") or "").strip())
    add("interface_type", (phys.get("InterfaceType") or "").strip())
    add("partition_style", (part.get("Type") or "").strip())
    add("free_bytes", ld.get("FreeSpace"))
    add("source_drive_letter", drive)

    return meta
