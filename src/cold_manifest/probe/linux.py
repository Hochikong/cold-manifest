"""Linux probe：lsblk -J 为主、blkid 兜底、smartctl 可选（§4.4）。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import DiskInfo, ProbeError, VolumeInfo, VolumeTarget

LSBLK_COLUMNS = "NAME,PATH,TYPE,FSTYPE,LABEL,UUID,SERIAL,MODEL,SIZE,PARTUUID,MOUNTPOINTS"


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise ProbeError(f"外部工具不可用：{cmd[0]}（{exc}）") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"外部工具超时：{' '.join(cmd)}") from exc
    if check and proc.returncode != 0:
        raise ProbeError(f"命令失败（rc={proc.returncode}）：{' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc


def find_mount_point(path: Path) -> tuple[str, str]:
    """从 /proc/mounts 找覆盖 path 的最长挂载点，返回 (mount_point, source_device)。"""
    try:
        raw = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ProbeError(f"无法读取 /proc/mounts：{exc}") from exc
    best_mp, best_src, best_len = "", "", -1
    want = str(path.resolve())
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        src, mp = parts[0], parts[1].replace("\\040", " ")
        if os.path.commonpath([want, mp]) == mp and len(mp) > best_len:
            best_mp, best_src, best_len = mp, src, len(mp)
    if not best_mp:
        raise ProbeError(f"未找到 {path} 所在挂载点")
    return best_mp, best_src


def parse_lsblk(text: str) -> list[dict]:
    """解析 lsblk -J 输出为扁平节点列表，每节点带 parent 键。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"lsblk 输出不是合法 JSON：{exc}") from exc
    flat: list[dict] = []

    def walk(nodes: list[dict], parent: dict | None) -> None:
        for node in nodes:
            node = dict(node)
            node["parent"] = parent
            flat.append(node)
            walk(node.get("children") or [], node)

    walk(data.get("blockdevices") or [], None)
    return flat


def _mounts_of(node: dict) -> list[str]:
    mps = node.get("mountpoints")
    if isinstance(mps, list):
        return [m for m in mps if m]
    if isinstance(mps, str):
        return [mps]
    return []


def select_target(nodes: list[dict], source_dev: str, mount_point: str) -> tuple[dict, dict | None]:
    """选出 source_dev / 挂载点对应的分区节点与其父盘节点。

    整盘文件系统（无分区表，如部分 LUKS/移动盘出厂格式）时挂载节点本身就是
    type=='disk'，此时该节点同时充当分区与磁盘。
    """
    src_name = os.path.basename(source_dev)
    part = next(
        (
            n
            for n in nodes
            if n.get("type") == "part"
            and (n.get("path") == source_dev or n.get("name") == src_name or _mounts_of(n) and mount_point in _mounts_of(n))
        ),
        None,
    )
    if part is None:
        part = next((n for n in nodes if mount_point in _mounts_of(n)), None)
    if part is None:
        raise ProbeError(f"lsblk 中未找到 {source_dev}（挂载于 {mount_point}）")
    if part.get("type") == "disk":
        return part, part
    disk = None
    parent = part.get("parent")
    while parent is not None:
        if parent.get("type") == "disk":
            disk = parent
            break
        parent = parent.get("parent")
    return part, disk


def enumerate_disk_volumes_linux(path: str) -> "tuple[list[VolumeTarget], list[str]]":
    """枚举 path 所在物理盘的全部可采集分区（lsblk -J 一棵树全搞定）。

    由 path 的挂载点定位分区 → 上溯到 disk → 枚举该盘下所有带文件系统的
    分区；swap / 无文件系统跳过，无挂载点跳过并记 warning。整盘文件系统
    （无分区表）时返回单卷。
    """
    p = Path(path)
    if not p.exists():
        raise ProbeError(f"路径不存在：{path}")

    mount_point, source_dev = find_mount_point(p)
    nodes = parse_lsblk(_run(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS]).stdout)
    part, disk = select_target(nodes, source_dev, mount_point)

    if disk is None or disk is part:
        # 整盘文件系统：单卷
        parts = [part]
    else:
        parts = [n for n in nodes if n.get("parent") is disk and n.get("type") == "part"]

    targets: list[VolumeTarget] = []
    warnings: list[str] = []
    seen_devices: set[str] = set()
    for n in parts:
        fstype = n.get("fstype") or ""
        if not fstype:
            continue  # 无文件系统（扩展分区/空分区）：静默跳过
        if fstype == "swap":
            continue
        mps = _mounts_of(n)
        if not mps:
            warnings.append(f"分区 {n.get('path') or n.get('name')} 无挂载点，跳过")
            continue
        dev = n.get("path") or n.get("name") or ""
        if dev in seen_devices:
            continue  # 同设备的多次挂载（bind mount，如 /mnt/wslg/distro）只取一个
        seen_devices.add(dev)
        # 请求路径所在的挂载点优先（该分区是定位起点时取真实挂载点，而非任意一个）
        mp = mount_point if mount_point in mps else mps[0]
        targets.append(
            VolumeTarget(
                path=mp,
                device_path=dev,
                partition_index=_partition_index(nodes, disk, n),
                filesystem=fstype,
                label=n.get("label") or "",
                capacity_bytes=n.get("size"),
                mount_point=mp,
            )
        )
    if not targets:
        raise ProbeError(f"{path} 所在盘没有可采集的已挂载卷")
    return targets, warnings


def parse_smartctl(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"smartctl 输出不是合法 JSON：{exc}") from exc


def _partition_index(nodes: list[dict], disk: dict | None, part: dict) -> int | None:
    if disk is None:
        return None
    sibs = [n for n in nodes if n.get("parent") is disk and n.get("type") == "part"]
    try:
        return sibs.index(part) + 1
    except ValueError:
        return None


def _table_type(part: dict, disk: dict | None) -> str:
    partuuid = part.get("partuuid") or ""
    ptuuid = (disk or {}).get("ptuuid") or ""
    if len(partuuid) == 36 or len(ptuuid) == 36:
        return "GPT"
    if len(ptuuid) == 8:
        return "MBR"
    return "unknown"


def _blkid_fallback(dev_path: str, volume: VolumeInfo) -> None:
    """lsblk 缺字段时用 blkid -o export 兜底 TYPE/LABEL/UUID。"""
    if volume.filesystem and volume.label:
        return
    proc = _run(["blkid", "-o", "export", dev_path], check=False)
    fields: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip()
    volume.filesystem = volume.filesystem or fields.get("TYPE", "")
    volume.label = volume.label or fields.get("LABEL", "")
    if not volume.partition_uuid:
        volume.partition_uuid = fields.get("UUID", "") or fields.get("PARTUUID", "")


def probe_path_linux(
    path: str, *, manual_serial: str | None = None, smartctl: bool = True
) -> tuple[VolumeInfo, DiskInfo]:
    p = Path(path)
    if not p.exists():
        raise ProbeError(f"路径不存在：{path}")

    mount_point, source_dev = find_mount_point(p)
    nodes = parse_lsblk(_run(["lsblk", "-J", "-b", "-o", LSBLK_COLUMNS]).stdout)
    part, disk = select_target(nodes, source_dev, mount_point)

    volume = VolumeInfo(
        filesystem=part.get("fstype") or "",
        label=part.get("label") or "",
        partition_uuid=part.get("partuuid") or "",
        partition_index=_partition_index(nodes, disk, part),
        partition_table_type=_table_type(part, disk),
        mount_point=mount_point,
        device_path=part.get("path") or source_dev,
    )
    _blkid_fallback(volume.device_path, volume)
    try:
        vfs = os.statvfs(mount_point)
        volume.free_bytes = vfs.f_bavail * vfs.f_frsize
        volume.capacity_bytes = part.get("size") or vfs.f_blocks * vfs.f_frsize
    except OSError:
        volume.capacity_bytes = part.get("size")

    disk_node = disk or {}
    lsblk_serial = (disk_node.get("serial") or "").strip()
    lsblk_model = (disk_node.get("model") or "").strip()
    disk_size = disk_node.get("size")

    physical_model = physical_serial = firmware = ""
    interface = ""
    smart_status = "unavailable"
    smart_serial = ""
    smart_raw: str | None = None
    smart_device_type = ""
    smart_error: str | None = None
    smart_error_raw: str | None = None
    if smartctl:
        disk_dev = disk_node.get("path") or os.path.basename(source_dev)
        # smart.py：CLDM_SMARTCTL 可配置 + CLDM_SMARTCTL_ARGS 透传 + -d sat 自动重试；
        # 拿不到 SMART（权限/不支持/超时）→ smart_error 记人话原因：不阻断采集
        from ..smart import read_smart_verbose

        res = read_smart_verbose(disk_dev)
        if res["ok"]:
            smart_raw = res["raw"]
            smart_device_type = res["device_type"]
            try:
                sj = parse_smartctl(smart_raw)
            except ProbeError:
                sj = {}
            physical_model = sj.get("model_name") or sj.get("device_model") or ""
            smart_serial = (sj.get("serial_number") or "").strip()
            firmware = sj.get("firmware_version") or ""
            interface = (sj.get("device") or {}).get("type") or ""
            passed = (sj.get("smart_status") or {}).get("passed")
            if passed is True:
                smart_status = "passed"
            elif passed is False:
                smart_status = "failed"
            else:
                # 无 smart_status 字段时按 exit_status 位掩码推断健康
                bits = smart_exit_status or 0
                if bits & 8:
                    smart_status = "failed"
                elif bits & (16 | 32 | 64 | 128):
                    smart_status = "warning"
                else:
                    smart_status = "unavailable"
        else:
            smart_error = res["message"]
            smart_error_raw = res["raw_excerpt"]

    if manual_serial:
        disk_serial, serial_source = manual_serial.strip(), "manual"
    elif smart_serial:
        disk_serial, serial_source = smart_serial, "smartctl"
    elif lsblk_serial:
        disk_serial, serial_source = lsblk_serial, "probe"
    else:
        disk_serial, serial_source = "", ""

    disk_info = DiskInfo(
        physical_model=physical_model,
        physical_serial=smart_serial,
        disk_serial=disk_serial,
        serial_source=serial_source,
        # Linux 无 USB 桥伪装问题时 lsblk model 即真盘；字段名沿用统一模型
        bridge_model=lsblk_model,
        interface_type=interface,
        capacity_bytes=disk_size,
        firmware=firmware,
        smart_status=smart_status,
        smart_raw=smart_raw,
        smart_device_type=smart_device_type,
        smart_error=smart_error,
        smart_error_raw=smart_error_raw,
    )
    return volume, disk_info
