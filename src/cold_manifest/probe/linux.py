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

    disk_info = DiskInfo(
        physical_model="",
        physical_serial="",
        disk_serial=lsblk_serial,
        serial_source="probe" if lsblk_serial else "",
        # Linux 无 USB 桥伪装问题时 lsblk model 即真盘；字段名沿用统一模型
        bridge_model=lsblk_model,
        interface_type="",
        capacity_bytes=disk_size,
        firmware="",
        smart_status="unavailable",
    )

    # smartctl 可选增强：设备定位走身份解析（PR-B，与 Windows 同口径）——
    # 按探测到的序列号/容量/型号在 smartctl 扫描表里校验身份，序列号覆盖
    # 改为有条件：已验证才覆盖；未验证只补全，否则保留 probe 值防错盘
    if smartctl:
        try:
            from ..smart import (parse_smart, read_smart_verbose,
                                 resolve_smart_device)

            probe_serial = disk_info.disk_serial
            cands, si = resolve_smart_device(
                expected_serial=probe_serial or None,
                expected_capacity_bytes=disk_info.capacity_bytes,
                expected_model=lsblk_model or None)
            amb = si.get("identity_ambiguity")
            mapped_by = si.get("mapped_by")
            verified = mapped_by in ("serial_match", "env_override") and not amb
            disk_info.identity_ambiguity = amb
            disk_info.identity_verified = verified
            disk_info.identity_risk = str(si.get("identity_risk")
                                          or (cands[0].get("risk") if cands else "")
                                          or "")
            warnings: list[str] = []
            if amb:
                warnings.append(f"设备身份歧义：{amb}")
            if si.get("identity_risk") == "target_not_found":
                # 已证明扫描表里没有目标盘：跳过 SMART 读取（宁可没有也不
                # 读错盘），保留 probe 序列号，原因落到 DiskInfo/scan_info
                disk_info.identity_risk = "target_not_found"
                disk_info.identity_reason = str(si.get("identity_reason") or "")
                warnings.append(f"已跳过 SMART 读取：{disk_info.identity_reason}")
                disk_info.identity_warnings = list(warnings)
                rsi = dict(si)
                rsi["identity_warnings"] = list(warnings)
                disk_info.smart_scan_info = rsi
            else:
                res = read_smart_verbose(devices=cands)
                disk_info.smart_attempts = res.get("attempts") or None
                rsi = dict(res.get("scan_info") or {})
                for k in ("mapped_by", "identity_risk", "identity_ambiguity"):
                    if si.get(k) is not None:
                        rsi[k] = si[k]
                if warnings:
                    rsi["identity_warnings"] = warnings
                disk_info.smart_scan_info = rsi
                if res["ok"]:
                    disk_info.smart_raw = res["raw"]
                    disk_info.smart_device_type = res["device_type"]
                    parsed = parse_smart(res["raw"],
                                         exit_status=res.get("exit_status"))
                    disk_info.physical_model = parsed.get("model") or ""
                    smart_serial = parsed.get("serial") or ""
                    if verified:
                        # 身份已验证（序列号精确匹配/环境覆盖且无歧义）：允许覆盖
                        if smart_serial:
                            disk_info.physical_serial = smart_serial
                            if probe_serial and smart_serial.strip().upper() \
                                    != probe_serial.strip().upper():
                                warnings.append(
                                    f"SMART 序列号 {smart_serial} 与探测值"
                                    f" {probe_serial} 不一致，已按验证结果覆盖")
                            disk_info.disk_serial = smart_serial
                            disk_info.serial_source = "smartctl"
                    elif smart_serial:
                        # 未验证：只允许"补全"，否则保留 probe 值防错盘
                        disk_info.physical_serial = smart_serial
                        if not probe_serial:
                            disk_info.disk_serial = smart_serial
                            disk_info.serial_source = "smartctl_unverified"
                            warnings.append(
                                "探测序列号为空，采用未经验证的 smartctl 序列号"
                                "（serial_source=smartctl_unverified）")
                        else:
                            warnings.append(
                                "设备身份未验证（smartctl 读到的设备未能按序列号"
                                f"确认为目标盘，读到序列号 {smart_serial}），"
                                "为防错盘已保留探测值")
                    disk_info.firmware = disk_info.firmware or (parsed.get("firmware") or "")
                    if disk_info.capacity_bytes is None and parsed.get("capacity_bytes"):
                        disk_info.capacity_bytes = parsed["capacity_bytes"]
                    disk_info.smart_status = parsed.get("health") or "unavailable"
                    disk_info.smart_exit_status = res.get("exit_status")
                    disk_info.interface_type = (parsed.get("device_type") or "") \
                        if parsed.get("device_type") else disk_info.interface_type
                else:
                    # 拿不到不阻断，但留下可诊断原因
                    disk_info.smart_error = res["message"]
                    disk_info.smart_error_raw = res["raw_excerpt"]
                if warnings:
                    disk_info.identity_warnings = list(warnings)
        except Exception:  # noqa: BLE001 — SMART 拿不到绝不阻断采集
            pass

    if manual_serial:
        disk_info.disk_serial = manual_serial.strip()
        disk_info.serial_source = "manual"
    return volume, disk_info
