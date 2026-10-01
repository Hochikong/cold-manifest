"""平台 probe 层（§4.4）：卷 / 物理盘元数据采集，Windows 与 Linux 双实现。

- Linux：lsblk -J（主）+ blkid（兜底）+ 可选 smartctl -i -H -A -j；
- Windows：PowerShell CIM/Storage cmdlet，返回单段 JSON 由解析器消费；
- 失败抛 ProbeError，无静默回退；smartctl 拿不到时 smart_status='unavailable'，不阻断。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field


class ProbeError(RuntimeError):
    """探测失败（找不到挂载点、外部命令失败、JSON 解析失败等）。"""


@dataclass
class VolumeInfo:
    filesystem: str = ""
    label: str = ""
    volume_serial_hex: str = ""
    partition_uuid: str = ""
    partition_index: int | None = None
    partition_table_type: str = "unknown"  # GPT / MBR / unknown
    capacity_bytes: int | None = None
    free_bytes: int | None = None
    mount_point: str = ""
    device_path: str = ""


@dataclass
class DiskInfo:
    physical_model: str = ""  # 真盘型号（smartctl 优先）
    physical_serial: str = ""  # 真盘序列号（smartctl 优先）
    disk_serial: str = ""  # 最终采用的序列号（volume_id 依据）
    serial_source: str = ""  # manual / smartctl / probe
    bridge_model: str = ""  # WMI/lsblk 报的型号（USB 桥下可能是桥）
    interface_type: str = ""
    capacity_bytes: int | None = None
    firmware: str = ""
    smart_status: str = "unavailable"  # passed / failed / unavailable
    smart_raw: str | None = None  # smartctl -A -j 原始 stdout（拿不到为 None）
    smart_device_type: str = ""  # smartctl -d 生效类型（"" = 默认参数即成功，"sat" = 重试生效）
    smart_error: str | None = None  # SMART 拿不到时的人话原因（可诊断）
    smart_error_raw: str | None = None  # 失败现场 stderr 片段（≤2KB，诊断用）
    smart_attempts: "list | None" = None  # smartctl 类型兜底链逐次尝试记录
    smart_exit_status: "int | None" = None  # 生效那次的 smartctl exit_status


@dataclass
class ProbeResult:
    volume: VolumeInfo
    disk: DiskInfo


@dataclass
class VolumeTarget:
    """多分区采集（§4.2）的单卷目标：path 可直接作为扫描根。"""

    path: str  # 挂载点（Linux "/mnt/x"）或盘符根（Windows "E:\\"）
    device_path: str = ""
    partition_index: "int | None" = None
    filesystem: str = ""
    label: str = ""
    capacity_bytes: "int | None" = None
    mount_point: str = ""


def enumerate_disk_volumes(
    path: str, *, smartctl: bool = True
) -> "tuple[list[VolumeTarget], list[str]]":
    """枚举 path 所在物理盘的全部可采集卷（多分区采集，§4.2）。

    返回 (targets, warnings)；跳过的分区（swap/无文件系统/无挂载点/无盘符）
    记入 warnings。按平台分派。
    """
    if sys.platform == "win32":
        from .windows import enumerate_disk_volumes_win

        return enumerate_disk_volumes_win(path)
    if sys.platform.startswith("linux"):
        from .linux import enumerate_disk_volumes_linux

        return enumerate_disk_volumes_linux(path)
    raise ProbeError(f"不支持的平台：{sys.platform}")


def probe_path(
    path: str, *, manual_serial: str | None = None, smartctl: bool = True
) -> tuple[VolumeInfo, DiskInfo]:
    """探测 path 所在卷与物理盘。按平台分派，macOS 不在支持范围。"""
    if sys.platform == "win32":
        from .windows import probe_path_win

        return probe_path_win(path, manual_serial=manual_serial, smartctl=smartctl)
    if sys.platform.startswith("linux"):
        from .linux import probe_path_linux

        return probe_path_linux(path, manual_serial=manual_serial, smartctl=smartctl)
    raise ProbeError(f"不支持的平台：{sys.platform}")
