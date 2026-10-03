"""在线采集编排（§4.3）：probe → scan → seal → on-disk copy → catalog 注册。

引擎与 CLI ``cldm collect``、Web 采集任务共用同一入口 ``collect_volume``。
快照库自包含（meta 自描述采集口径），封库逻辑与 import_legacy 共用 seal.py。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .catalog import (connect_catalog, create_batch, ensure_disk, ensure_volume,
                      find_disk_by_identity, find_probe_collision,
                      find_volume_by_partition, preferred_disk_serial,
                      register_snapshot, rmtree_ro, snapshot_path,
                      validate_volume_id, _sanitize_serial)
from .paths import normalize_path
from .probe import DiskInfo, ProbeError, VolumeInfo, probe_path
from .scanner import ScanCancelled, scan_tree
from .scan_journal import ScanJournal, journal_path, read_completed
from .schema import SNAPSHOT_TABLES_DDL
from .seal import is_collect_orphan, is_sealed, seal_snapshot

# 盘上副本的空间余量：库大小 + 64 MiB（旁车与临时空间）
_COPY_MARGIN_BYTES = 64 * 1024 * 1024
_BATCH_SIZE = 10_000

ProgressCB = Callable[[str, int, "int | None"], None]

_log = logging.getLogger(__name__)


class CollectError(Exception):
    """采集失败（probe 失败、非法 volume_id、目标已存在等）。"""


class CollectCancelled(CollectError):
    """采集被取消。取消=丢弃本次：目标目录与临时文件由引擎清理，不做续采。"""


class ManualSerialRequired(CollectError):
    """读不到任何可用序列号（ATA 直通与系统枚举皆占位）且未手填：拒绝提交采集。"""


def _is_usb_bridge(disk: DiskInfo) -> bool:
    """判定是否经 USB 硬盘盒接入：interface_type 含 USB（大小写不敏感）。

    注意：新口径下此函数**只用于提示文案**（是否提到"经 USB 硬盘盒接入"），
    不再参与是否强制手填序列号的判定（见 requires_manual_serial）。
    不能拿 bridge_model 非空当判据——Linux probe 会把 lsblk 的盘型号
    无条件填进 bridge_model，非 USB 盘会被误判。bridge_model 仅作提示展示。
    """
    it = (getattr(disk, "interface_type", "") or "").upper()
    return "USB" in it


def _is_windows() -> bool:
    """运行平台是否 Windows（可被测试 monkeypatch 以构造 Windows 判定夹具）。"""
    return sys.platform == "win32" or sys.platform.startswith("win")


def _smartctl_verified_serial(disk: DiskInfo) -> str:
    """取"已验证的 smartctl 真序列号"：仅当 probe 判定 serial_source=smartctl
    （设备身份已按序列号验证通过）时才返回该值，否则空串。

    provenance 判据说明（Windows probe 现有字段，选最可靠的一种）：
    - ``serial_source == "smartctl"``：smartctl 读到序列号**且**设备映射已验证，
      这是唯一可信来源（用户真机证实 smartctl -i -j C: 与 /dev/sdX 一致）；
    - ``serial_source == "smartctl_unverified"``：smartctl 读到了序列号但**未能
      确认读到的是目标盘**——可能是另一块盘的序列号，宁严勿松，不放行；
    - ``serial_source == "probe"``：系统枚举（Get-Disk / WMI）上报值，对 USB 盒
      常是盒子 ID / 占位号（如 20260123004775F、0123456789ABCDEF），一律不采信。
    """
    if (getattr(disk, "serial_source", "") or "").strip() == "smartctl":
        return (getattr(disk, "disk_serial", "") or "").strip()
    return ""


def requires_manual_serial(disk: DiskInfo, *, windows: "bool | None" = None) -> bool:
    """是否必须手填序列号（唯一判据，collect_volume 与 preflight 共用）：

    **Windows（2026-10 用户硬规则收紧）**：读不到 smartctl **已验证**的真序列号
    （serial_source=smartctl）时，禁止用 Get-Disk/WMI 的系统枚举值充当磁盘身份
    ——即使它看起来"可用"（如 16NDT0O1T）也必须手填；手填值（serial_source=
    manual）放行，``--volume-id`` 不放行。系统枚举值仍可展示（preflight 的
    probe_serial），但不参与任何判定。

    **非 Windows（Linux/macOS）行为不变**：ATA 直通（physical_serial）或系统
    枚举（disk_serial）之一可用即放行，仅两者皆不可用才强制手填。
    """
    if (getattr(disk, "serial_source", "") or "").strip() == "manual":
        return False  # 手填值始终满足要求（现状不变）
    if windows is None:
        windows = _is_windows()
    if windows:
        # Windows 新口径：只认已验证的 smartctl 真序列号
        return not _usable_serial(_smartctl_verified_serial(disk))
    # Linux/macOS：维持原判据（physical 优先，见 catalog.preferred_disk_serial）
    phys = (getattr(disk, "physical_serial", "") or "").strip()
    sys_serial = (getattr(disk, "disk_serial", "") or "").strip()
    return not (_usable_serial(phys) or _usable_serial(sys_serial))


def serial_required_message(disk: DiskInfo, volume_serial_hex: "str | None",
                            manual_serial: "str | None",
                            *, windows: "bool | None" = None) -> "str | None":
    """判定是否必须手填序列号；是则返回人话提示，None=可继续采集。

    规则：requires_manual_serial(disk) 为真 → 返回提示（Windows 上新口径为
    "读不到已验证的 smartctl 真序列号"；非 Windows 为"两个来源都不可用"）。
    有 USB 盒信息时补充"该盘经 USB 硬盘盒接入"的上下文说明，但 USB 与否
    不影响判定。volume_serial_hex 形参保留兼容签名：VOL-{卷序列号hex} 回退
    命名仍在 collect_volume 里保留，但在本判定下实际不可达（都会先被拦）。
    """
    if manual_serial and _usable_serial(manual_serial):
        return None
    if windows is None:
        windows = _is_windows()
    if not requires_manual_serial(disk, windows=windows):
        return None
    phys = (getattr(disk, "physical_serial", "") or "").strip()
    probe_serial = (getattr(disk, "disk_serial", "") or "").strip()
    src = (getattr(disk, "serial_source", "") or "").strip()
    parts: list[str] = []
    if windows:
        # Windows 新口径文案：系统枚举值不被采信为身份
        parts.append("该盘读不到 smartctl 验证过的真序列号")
        if src == "smartctl_unverified" and phys:
            parts.append(f"smartctl 读到序列号 {phys!r} 但设备身份未验证"
                         "（无法确认读到的是目标盘），不采信")
        elif probe_serial:
            parts.append(f"系统枚举（Get-Disk/WMI）上报的 {probe_serial!r} "
                         "可能是盒子/桥的 ID，不允许充当磁盘身份")
        else:
            parts.append("未能读到任何序列号")
    else:
        parts.append("该盘读不到可用的序列号")
        if phys:
            parts.append(f"ATA 直通读到的序列号 {phys!r} 不可用（空串/全 0/占位）")
        if probe_serial:
            parts.append(f"系统枚举（Windows 枚举）上报的序列号 {probe_serial!r} 不可用"
                         "（空串/全 0/占位，可能是盒子/桥的 ID）")
        if not phys and not probe_serial:
            parts.append("未能读到任何序列号")
    if _is_usb_bridge(disk):
        desc = ((getattr(disk, "bridge_model", "") or "").strip()
                or (getattr(disk, "interface_type", "") or "").strip() or "USB")
        parts.append(f"该盘经 USB 硬盘盒接入（盒子：{desc}）")
    parts.append("请在采集时手填盘体标签上的序列号"
                 "（CLI: `--serial`，Web: \"磁盘序列号\"输入框）")
    parts.append("显式 --volume-id 只固定本次卷命名，"
                 "不能替代序列号作为磁盘身份（catalog 盘身份与复用判重都依赖序列号），"
                 "因此不因此放行")
    return "；".join(parts)


def preflight_serial_required(scan_root: "str | Path", *, manual_serial: "str | None" = None,
                              smartctl: bool = True) -> "str | None":
    """提交前预检（API/CLI 批次用）：probe 一次并判定是否必须手填序列号。

    返回提示文本（需手填）或 None（可继续）。probe 失败返回 None——
    probe 问题由采集阶段统一报错，预检不重复报。
    """
    try:
        vol, disk = probe_path(str(scan_root), manual_serial=manual_serial,
                               smartctl=smartctl)
    except ProbeError:
        return None
    return serial_required_message(disk, getattr(vol, "volume_serial_hex", None),
                                   manual_serial)


@dataclass
class CollectResult:
    snapshot_id: str
    volume_id: str
    db_path: Path
    files: int
    dirs: int
    symlinks: int
    others: int
    total_bytes: int
    total_allocated: int
    skipped: int
    elapsed_s: float
    host_sha256: str
    on_disk_path: Path | None
    on_disk_sha256: str | None
    resumed: bool = False
    warnings: list[str] = field(default_factory=list)


class _SQLiteEntryWriter:
    """scan_tree → entries/skipped 的批量写入适配器（万行批事务）。"""

    def __init__(self, conn: sqlite3.Connection, resume: bool = False) -> None:
        self._conn = conn
        self._resume = resume
        self._buf: list[tuple] = []
        self._skipped_buf: list[tuple] = []

    def add_entry(self, *, parent_id: int, path: str, name: str, depth: int, type: str,
                  size_bytes: int | None, allocated_bytes: int | None, mtime_ns: int | None,
                  ctime_ns: int | None, btime_ns: int | None, attrs: int | None, ext: str,
                  error: str | None = None) -> int:
        vals = (parent_id, path, name, depth, type, size_bytes, allocated_bytes,
                mtime_ns, ctime_ns, btime_ns, attrs, ext, normalize_path(path), error)
        if self._resume:
            # 断点续采重扫：安全重扫已部分入库的目录。取舍：用 INSERT OR IGNORE
            # （而非"按子树先清理再重插"——重扫库未封库、path 索引在续采开始时
            # 一次性补建，清理式 DELETE 需逐目录全表扫 path 前缀，代价更高且
            # 会丢已入库行的稳定 entry_id）；被忽略的既有行按 path 查回
            # entry_id，父目录引用因此正确回填。
            self._conn.execute(
                "INSERT OR IGNORE INTO entries(parent_id,path,name,depth,type,size_bytes,"
                "allocated_bytes,mtime_ns,ctime_ns,btime_ns,attrs,ext,path_norm,error)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
            row = self._conn.execute(
                "SELECT entry_id FROM entries WHERE path=?", (path,)).fetchone()
            eid = row[0]
        else:
            eid = self._conn.execute(
                "INSERT INTO entries(parent_id,path,name,depth,type,size_bytes,allocated_bytes,"
                "mtime_ns,ctime_ns,btime_ns,attrs,ext,path_norm,error)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals).lastrowid
            assert eid is not None
        self._buf.append(eid)
        if len(self._buf) >= _BATCH_SIZE:
            self._conn.commit()
            self._buf.clear()
        return eid

    def add_skipped(self, path: str, warning_type: str, stage: str, detail: str | None = None) -> None:
        self._skipped_buf.append((path, warning_type, stage, detail))
        if len(self._skipped_buf) >= _BATCH_SIZE:
            self._flush_skipped()

    def _flush_skipped(self) -> None:
        if self._skipped_buf:
            self._conn.executemany(
                "INSERT OR IGNORE INTO skipped(path,warning_type,stage,detail) VALUES(?,?,?,?)",
                self._skipped_buf)
            self._skipped_buf.clear()


def _sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _copy_with_hash(src: Path, dst: Path, cancel_event: "threading.Event | None" = None,
                    progress_cb: "Callable[[int, int], None] | None" = None,
                    total_bytes: "int | None" = None) -> str:
    """流式拷贝 src → dst，同时计算 sha256（单遍读）。

    cancel_event：块循环逐块检查（CollectCancelled）；progress_cb(done_bytes, total_bytes)。
    """
    h = hashlib.sha256()
    done = 0
    with src.open("rb") as fin, dst.open("wb") as fout:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CollectCancelled("采集已取消（copy 阶段）")
            block = fin.read(4 * 1024 * 1024)
            if not block:
                break
            h.update(block)
            fout.write(block)
            done += len(block)
            if progress_cb is not None:
                progress_cb(done, total_bytes if total_bytes is not None else done)
    return h.hexdigest()


def _write_meta(conn: sqlite3.Connection, scan_root: Path, volume_id: str,
                vol: VolumeInfo, disk: DiskInfo, ts: str,
                exclude_globs: list[str] | None, exclude_hidden: bool,
                include_system: bool, cross_filesystems: bool = False,
                serial_fallback: bool = False,
                probe_serial_raw: str = "") -> None:
    """§4.1 盘/卷/采集口径全量 meta；拿不到的字段写 NULL。"""

    def s(v) -> str | None:
        return None if v is None or v == "" else str(v)

    smart_raw = getattr(disk, "smart_raw", None)
    smart_exit_status = getattr(disk, "smart_exit_status", None)
    smart_struct: dict = {}
    if smart_raw:
        raw_text = smart_raw if isinstance(smart_raw, str) else json.dumps(smart_raw)
        try:
            from .smart import parse_smart

            smart_struct = parse_smart(raw_text, exit_status=smart_exit_status)
        except Exception:  # noqa: BLE001 — 结构化字段失败不阻断采集
            smart_struct = {}
    meta: dict[str, str | None] = {
        "volume_id": volume_id,
        "disk_serial": s(disk.disk_serial),
        "serial_source": s(disk.serial_source),
        "physical_model": s(disk.physical_model),
        "physical_serial": s(disk.physical_serial),
        "bridge_model": s(disk.bridge_model),
        "interface_type": s(disk.interface_type),
        "disk_capacity_bytes": s(disk.capacity_bytes),
        "firmware": s(disk.firmware),
        "smart_status": s(disk.smart_status),
        "filesystem": s(vol.filesystem),
        "volume_label": s(vol.label),
        "volume_serial_hex": s(vol.volume_serial_hex),
        "partition_uuid": s(vol.partition_uuid),
        "partition_index": s(vol.partition_index),
        "partition_table_type": s(vol.partition_table_type),
        "partition_capacity_bytes": s(vol.capacity_bytes),
        "free_bytes": s(vol.free_bytes),
        "source_mount": s(vol.mount_point),
        "os_platform": sys.platform,
        "scan_root": str(scan_root),
        # 与 scan_root 同值：host_path 语义修复（§4.5）后冗余一份，便于诊断/迁移
        "root_path": str(scan_root),
        "exclude_rules_json": json.dumps({
            "exclude_globs": list(exclude_globs or []),
            "exclude_hidden": exclude_hidden,
            "include_system": include_system,
        }, ensure_ascii=False),
        # 扫描口径旗标独立成键（不含"排除"语义，避免塞进 exclude_rules_json）
        "scan_flags_json": json.dumps({"cross_filesystems": cross_filesystems}),
        "hash_policy": "none",
        "collect_time_utc": datetime.strptime(ts, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ"),
        "collector_version": __version__,
    }
    if smart_raw:
        meta["smart_raw_json"] = smart_raw if isinstance(smart_raw, str) else json.dumps(smart_raw)
        # 结构化字段（P4-②）：smart_* 前缀；缺失/None 不写键
        for key in ("health", "temperature_c", "power_on_hours", "reallocated_ct",
                    "pending_ct", "start_stop_ct", "spin_up_ms", "device_type",
                    "capacity_bytes", "rotation_rate", "form_factor"):
            v = smart_struct.get(key)
            if v is not None:
                meta[f"smart_{key}"] = str(v)
        ssd = smart_struct.get("ssd") or {}
        if ssd:
            meta["smart_ssd_json"] = json.dumps(ssd, ensure_ascii=False)
        # ATA 属性整表一份 JSON（关键指标走 smart_* 扁平键，不塞 20 个 meta 键）
        ata_attrs = smart_struct.get("ata_attributes")
        if isinstance(ata_attrs, list) and ata_attrs:
            meta["smart_ata_json"] = json.dumps(ata_attrs, ensure_ascii=False)
    else:
        # 拿不到 SMART 时留下可诊断原因（成功路径不受影响）
        smart_error = getattr(disk, "smart_error", None)
        if smart_error:
            meta["smart_error"] = str(smart_error)
            raw_err = getattr(disk, "smart_error_raw", None)
            if raw_err:
                meta["smart_error_raw"] = str(raw_err)[:2048]
    # 类型兜底链逐次尝试记录（诊断用：成功/失败都可能有失败尝试）
    smart_attempts = getattr(disk, "smart_attempts", None)
    if smart_attempts:
        meta["smart_attempts_json"] = json.dumps(smart_attempts, ensure_ascii=False)
    # 设备映射结论（诊断用：扫描表/候选链/生效设备串）
    smart_scan_info = getattr(disk, "smart_scan_info", None)
    if smart_scan_info:
        meta["smart_scan_info_json"] = json.dumps(smart_scan_info, ensure_ascii=False)
    # 设备身份校验结论（PR-B）：仅在有值时写
    identity_verified = getattr(disk, "identity_verified", None)
    if identity_verified is not None:
        meta["identity_verified"] = "1" if identity_verified else "0"
    identity_risk = getattr(disk, "identity_risk", None)
    if identity_risk:
        meta["identity_risk"] = str(identity_risk)
    identity_ambiguity = getattr(disk, "identity_ambiguity", None)
    if identity_ambiguity:
        meta["identity_ambiguity"] = str(identity_ambiguity)
    identity_warnings = getattr(disk, "identity_warnings", None)
    if identity_warnings:
        meta["identity_warnings_json"] = json.dumps(identity_warnings,
                                                    ensure_ascii=False)
    if serial_fallback:
        # 序列号回退时的原始探测值（诊断用：说明为何 volume_id 用了卷序列号）
        meta["probe_serial_raw"] = probe_serial_raw
    conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                     list(meta.items()))


def _is_sealed(db_path: Path) -> bool:
    """快照库是否已封库。与 import_legacy / 任务层孤儿清扫同口径（seal.is_sealed）。"""
    return is_sealed(db_path)


def _find_resume_candidate(vol_root: Path) -> "Path | None":
    """断点续采候选：该 volume 下**最新**的未封库且带 scan_journal 的 ts 目录。

    带侧车 journal 的未封库目录视为可续采（清扫也按此口径保留，见
    seal.is_collect_orphan）；封库后的目录不可续采。
    """
    if not vol_root.is_dir():
        return None
    best: "Path | None" = None
    try:
        children = sorted(vol_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return None
    for child in children:
        if not child.is_dir():
            continue
        if not journal_path(child).is_file():
            continue
        db = child / "snapshot.db"
        if not db.is_file() or _is_sealed(db):
            continue
        best = child  # ts 目录名可排序，取最新
    return best


def _sweep_leftovers(data_root: Path, volume_id: str, keep_ts: str,
                     warnings: list[str]) -> None:
    """清扫同 volume_id 下未 sealed 的残留目录（上次采集崩溃留下的脏库）。

    孤儿判定与任务层共用 seal.is_collect_orphan（含 collector_version 守卫与
    断点续采口径：带 scan_journal 的未封库目录可续采，保留不删；
    外部/测试库不动）。keep_ts（本次目标）始终跳过。
    """
    vol_root = data_root / volume_id
    if not vol_root.is_dir():
        return
    for child in vol_root.iterdir():
        if not child.is_dir() or child.name == keep_ts:
            continue
        if not is_collect_orphan(child):
            continue  # 已封库的历史快照或非本工具产生的库，不动
        try:
            rmtree_ro(child)
        except OSError as exc:
            warnings.append(f"leftover_cleanup=failed({child}: {exc})")
        else:
            warnings.append(f"leftover_cleaned={child.name}")


def _copy_to_disk(scan_root: Path, volume_id: str, ts: str, db_path: Path,
                  stats: dict, warnings: list[str],
                  host_sha256: str,
                  cancel_event: "threading.Event | None" = None,
                  copy_progress: "Callable[[int, int], None] | None" = None,
                  ) -> tuple[Path | None, str | None]:
    """§4.6 盘上副本：``<scan_root>/_coldmanifest/<volume_id>/<ts>/snapshot.db`` + snapshot.json。

    返回 (副本路径或 None, 副本 sha256 或 None)；空间不足/失败只记 warning，不抛异常。
    copy_progress(done_bytes, total_bytes)：按字节的拷贝进度。
    """
    db_size = db_path.stat().st_size
    try:
        free = shutil.disk_usage(scan_root).free
    except OSError as exc:
        warnings.append(f"on_disk_copy=skipped(disk_usage: {exc})")
        return None, None
    if free < db_size + _COPY_MARGIN_BYTES:
        warnings.append("on_disk_copy=skipped(no_space)")
        return None, None

    copy_dir = Path(scan_root) / "_coldmanifest" / volume_id / ts
    tmp = copy_dir / "snapshot.db.tmp"
    final = copy_dir / "snapshot.db"
    try:
        copy_dir.mkdir(parents=True, exist_ok=True)
        # 残留 .tmp 可重跑：直接覆盖；sha256 复用封库时已算好的主机哈希
        host_hash = host_sha256
        copy_hash = _copy_with_hash(db_path, tmp, cancel_event=cancel_event,
                                    progress_cb=copy_progress, total_bytes=db_size)
        if copy_hash != host_hash:
            tmp.unlink(missing_ok=True)
            warnings.append("on_disk_copy=failed(sha_mismatch)")
            return None, None
        os.replace(tmp, final)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        warnings.append(f"on_disk_copy=failed({exc})")
        return None, None

    sidecar = {
        "volume_id": volume_id,
        "collect_time": datetime.strptime(ts, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": stats["file_count"],
        "total_bytes": stats["total_bytes"],
        "collector_version": __version__,
    }
    try:
        (copy_dir / "snapshot.json").write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        warnings.append(f"on_disk_sidecar=failed({exc})")
    return final, copy_hash


def _register_catalog(data_root: Path, snapshot_id: str, volume_id: str,
                      vol: VolumeInfo, disk: DiskInfo, stats: dict,
                      exclude_globs: list[str] | None, exclude_hidden: bool,
                      on_disk_copy: bool,
                      on_disk_path: Path | None, on_disk_sha256: str | None,
                      collected_at: str, warnings: list[str],
                      root_path: Path,
                      cross_filesystems: bool = False) -> str:
    """catalog 注册：disk/volume/batch/snapshot + on_disk_copies。返回 batch_id。

    磁盘行按身份查找复用（catalog.find_disk_by_identity：物理序列号优先、
    其次探测序列号，probe 撞车且物理序列号不同 → 不复用）：找得到沿用既有
    disk_id（历史不变），找不到按"物理序列号优先"新建 —— 两块报出相同
    探测序列号的盘（USB 盒常见）由此各自成行，不再共用一个盘/卷。
    """
    serial = (disk.disk_serial or "").strip()
    cat = connect_catalog(data_root)
    try:
        physical = getattr(disk, "physical_serial", None) or None
        existing_disk_id = find_disk_by_identity(cat, probe_serial=serial,
                                                 physical_serial=physical)
        collision_disk_id = None
        if existing_disk_id is None:
            # probe 撞车（两块盘报相同探测序列号）：本次盘按物理序列号另立新行，
            # 冲突记录照旧写到既有盘行上（identity_verified=0 + 冲突历史）
            collision_disk_id = find_probe_collision(
                cat, probe_serial=serial, physical_serial=physical)
        name_serial = preferred_disk_serial(physical, serial)
        disk_id = existing_disk_id or name_serial or f"NOSERIAL_{volume_id}"

        def _disk_fields() -> dict:
            return dict(physical_model=disk.physical_model or None,
                        physical_serial=physical,
                        bridge_model=disk.bridge_model or None,
                        capacity_bytes=disk.capacity_bytes,
                        interface_type=disk.interface_type or None,
                        identity_verified=(
                            (1 if disk.identity_verified else 0)
                            if getattr(disk, "identity_verified", None)
                            is not None else None))

        if collision_disk_id:
            ensure_disk(cat, collision_disk_id, **_disk_fields())
        reg = ensure_disk(cat, disk_id, **_disk_fields())
        if reg["conflicts"] or collision_disk_id:
            fields_seen = "、".join(
                c["field"] for c in (reg["conflicts"] or
                                     [{"field": "physical_serial"}]))
            warnings.append(
                f"磁盘身份冲突：disk_id {collision_disk_id or disk_id} 已有记录"
                f"与本次探测不一致（{fields_seen}）；同 ID 但物理序列号不同，"
                f"已按物理序列号区分（各自独立盘/卷行），"
                f"若两块盘仍共用请显式 --serial 区分")
        ensure_volume(cat, volume_id, disk_id,
                      partition_index=vol.partition_index,
                      partition_uuid=vol.partition_uuid or None,
                      volume_serial_hex=vol.volume_serial_hex or None,
                      filesystem=vol.filesystem or None,
                      label=vol.label or None,
                      capacity_bytes=vol.capacity_bytes)
        batch_id = create_batch(cat, disk_id, [volume_id])
        register_snapshot(cat, snapshot_id, volume_id,
                          batch_id=batch_id,
                          collected_at=collected_at,
                          collector_version=__version__,
                          host_path=str(root_path),
                          status="sealed", hash_policy="none",
                          exclude_rules_json=json.dumps({
                              "exclude_globs": list(exclude_globs or []),
                              "exclude_hidden": exclude_hidden,
                              "cross_filesystems": cross_filesystems,
                          }, ensure_ascii=False),
                          file_count=stats["file_count"], dir_count=stats["dir_count"],
                          total_bytes=stats["total_bytes"], total_alloc=stats["total_alloc"],
                          zero_byte_count=stats["zero_byte_count"], max_depth=stats["max_depth"],
                          skipped_count=stats["skipped_count"])
        if on_disk_copy:
            # 仅在真正尝试拷贝时插行；--no-on-disk-copy 关闭时不插、不写 skipped_no_space
            cat.execute(
                "INSERT INTO on_disk_copies(snapshot_id, disk_path, copied_at, sha256, verified_at, status)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(snapshot_id) DO UPDATE SET disk_path=excluded.disk_path,"
                " copied_at=excluded.copied_at, sha256=excluded.sha256,"
                " verified_at=excluded.verified_at, status=excluded.status",
                (snapshot_id,
                 str(on_disk_path) if on_disk_path else None,
                 collected_at if on_disk_path else None,
                 on_disk_sha256,
                 collected_at if on_disk_path else None,
                 "ok" if on_disk_path else "skipped_no_space"))
        # SMART 历史落库（P4-②）：失败只 warning，不阻断注册
        try:
            from .smart import parse_smart, record_smart

            smart_raw = getattr(disk, "smart_raw", None)
            if smart_raw:
                raw_text = smart_raw if isinstance(smart_raw, str) else json.dumps(smart_raw)
                record_smart(cat, disk_id, snapshot_id,
                             parse_smart(raw_text,
                                         exit_status=getattr(disk, "smart_exit_status", None)),
                             collected_at=collected_at, raw_json=raw_text)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"smart_record=failed({e})")
        cat.commit()
    finally:
        cat.close()
    return batch_id


def _usable_serial(serial: "str | None") -> bool:
    """已上移为公共工具：见 catalog._usable_serial（保留别名供既有调用）。"""
    from .catalog import _usable_serial as _fn
    return _fn(serial)


def _reuse_existing_volume_id(data_root: "str | Path", disk: DiskInfo,
                              probe_serial: str,
                              index: "int | None") -> "str | None":
    """既有卷复用：该盘（按身份查）已有同分区号的卷 → 沿用其 volume_id。

    找不到盘、或盘下无同分区号卷（新分区）→ None（由 caller 按命名规则新建）。
    catalog 打不开等异常按 None 处理（后续注册阶段照常报错，不在此吞成硬失败）。
    """
    try:
        cat = connect_catalog(data_root)
    except Exception:  # noqa: BLE001
        return None
    try:
        disk_id = find_disk_by_identity(
            cat, probe_serial=probe_serial,
            physical_serial=getattr(disk, "physical_serial", None) or None)
        if disk_id is None:
            return None
        return find_volume_by_partition(cat, disk_id=disk_id,
                                        partition_index=index)
    except Exception:  # noqa: BLE001
        return None
    finally:
        cat.close()


def collect_volume(scan_root: "str | Path", *, data_root: "str | Path",
                   volume_id: str | None = None, manual_serial: str | None = None,
                   exclude_globs: list[str] | None = None, exclude_hidden: bool = False,
                   include_system: bool = True, smartctl: bool = True,
                   on_disk_copy: bool = True, resume: bool = False,
                   cross_filesystems: bool = False,
                   cancel_event: "threading.Event | None" = None,
                   progress_cb: ProgressCB | None = None) -> CollectResult:
    """采集一个卷：probe → scan → seal → 盘上副本 → catalog 注册。

    progress_cb(phase, done, total)，phase ∈ {"probe","scan","seal","copy","register","done"}；
    scan 阶段 total=None（流式，总数未知）；copy 按字节回调；seal 拆
    index/rollup(depth 多步)/optimize 细粒度步骤。取消检查在 scanner 目录
    粒度与 copy 块循环。

    断点续采（§4.3）：扫描开始前建 scan_journal 侧车并在 meta 写
    resume_state='incomplete'；中断（崩溃/取消/失败）后未封库目录保留。
    resume=True 时在 <data_root>/<volume_id>/ 下找最新可续采目录就地续采
    （沿用其 ts/db/journal，跳过已完成子树、OR IGNORE 安全重扫未记录部分）；
    找不到则照常新建。续采完成的库与一次完整扫描等效。封库成功后
    resume_state 改写为 'complete'（保留键不删除：可区分"采集全程无中断"
    之外的状态轨迹，便于诊断）。

    cross_filesystems：默认 False（one_filesystem 剪枝）——扫描根以外的
    文件系统（挂载点如 /proc、/sys、网络盘）整棵跳过并记 skipped。
    True 时跨入子挂载点（口径记入 meta scan_flags_json）。
    """
    t0 = time.monotonic()
    warnings: list[str] = []
    # resolve：host_path/root_path 存被扫描根的绝对路径（多分区=该卷挂载根），
    # 供按需哈希定位源文件（此前误存 snapshot.db 路径，哈希必失败）
    scan_root = Path(scan_root).resolve()
    if not scan_root.is_dir():
        raise CollectError(f"扫描根不存在或不是目录：{scan_root}")
    data_root = Path(data_root)

    # 长路径预警（P2-2）：仅 Windows 且扫描根路径超长时提醒；Linux 不触发。
    # 仅警告不阻断——深层文件是否真的超 MAX_PATH 取决于实际目录深度。
    long_path_warning = (
        sys.platform == "win32" and len(str(scan_root)) > 200)
    if long_path_warning:
        warnings.append(
            "long_path=扫描根路径超过 200 字符：建议启用 Windows 长路径支持"
            "（组策略/注册表 LongPathsEnabled），否则深层文件可能因 MAX_PATH 限制采集失败")

    def _check_cancel() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise CollectCancelled("采集已取消")

    if progress_cb is not None:
        progress_cb("probe", 0, 1)
    try:
        vol, disk = probe_path(str(scan_root), manual_serial=manual_serial, smartctl=smartctl)
    except ProbeError as e:
        raise CollectError(f"probe 失败：{e}") from e
    if progress_cb is not None:
        progress_cb("probe", 1, 1)

    # 手填序列号强制检查（硬盘盒/占位序列号）：probe 后、scan 前，不满足直接拒绝
    _serial_msg = serial_required_message(disk, getattr(vol, "volume_serial_hex", None),
                                          manual_serial)
    if _serial_msg:
        raise ManualSerialRequired(_serial_msg)

    serial_fallback = False
    probe_serial_raw = ""
    if volume_id is None:
        serial = (disk.disk_serial or "").strip()
        index = vol.partition_index if vol.partition_index is not None else 0
        if not _usable_serial(serial):
            # 系统枚举序列号不可用 → ATA 直通真盘序列号优先（现有优先级
            # physical 优先，与 preferred_disk_serial 一致）。
            phys = (getattr(disk, "physical_serial", "") or "").strip()
            if _usable_serial(phys):
                probe_serial_raw = serial
                serial = _sanitize_serial(phys)
                warnings.append(
                    f"系统枚举序列号不可用（探测值={probe_serial_raw!r}），"
                    f"已改用 ATA 直通真盘序列号命名：{serial}")
            else:
                # VOL-{卷序列号hex} 回退路径保留但实际不可达：系统枚举与
                # ATA 直通序列号都不可用时，前面已被 ManualSerialRequired
                # 拦截（要求手填），走到这里说明 manual_serial 已覆盖
                # disk_serial（usable），不会进入本分支。
                vhex = (vol.volume_serial_hex or "").strip()
                if _usable_serial(vhex):
                    volume_id = f"VOL-{vhex.upper()}_P{index}"
                    probe_serial_raw = serial
                    serial_fallback = True
                    disk.serial_source = "volume_serial_fallback"
                    disk.disk_serial = ""  # 保持真实：占位值不伪造为盘序列号
                    warnings.append(
                        f"未能读取盘序列号（探测值={serial!r}，USB 桥常见），"
                        f"已回退用卷序列号命名 volume_id：{volume_id}（建议显式 --serial 固定命名）")
                    if _is_usb_bridge(disk) and not manual_serial:
                        _box = ((getattr(disk, "bridge_model", "") or "").strip()
                                or (getattr(disk, "interface_type", "") or "").strip()
                                or "USB")
                        warnings.append(
                            f"该盘经 USB 硬盘盒接入（盒子：{_box}），卷序列号不能唯一标识"
                            f"盘体；强烈建议显式 --serial 手填盘体标签序列号后重新采集")
                else:
                    # 兜底（理论上不可达）：可用手填序列号会在 probe 阶段覆盖
                    # disk_serial；两者都不可用已被 ManualSerialRequired 拒绝
                    raise CollectError(
                        "未能读取盘序列号（USB 桥常见），请显式指定 `--serial <值>`"
                        "（或用 --volume-id 手动命名该卷）")
        else:
            # 怪异序列号（空格/斜杠/Unicode 等）清洗为合法 volume_id 字符，再统一校验
            serial = _sanitize_serial(serial)
        if volume_id is None:
            # 优先复用既有盘/卷：同一块盘再采 → 沿用原 volume_id（历史稳定）；
            # probe 撞车（两块盘报出相同探测序列号）且物理序列号可区分 → 不复用。
            # 显式 --serial 时跳过复用（用户指定的命名优先）
            if manual_serial is None:
                volume_id = _reuse_existing_volume_id(data_root, disk, serial, index)
            if volume_id is None:
                # 新卷命名：显式 --serial 最高优先；其次物理序列号可用优先
                # （probe 撞车时两块盘各自成卷），再次探测序列号
                name_serial = (_sanitize_serial(manual_serial) if manual_serial
                               else preferred_disk_serial(
                                   getattr(disk, "physical_serial", None), serial))
                volume_id = f"{name_serial}_P{index}"
    try:
        validate_volume_id(volume_id)
    except Exception as e:
        # 含 LegacyImportError：CLI 层只认 CollectError（退出码 2，不裸 traceback）
        raise CollectError(str(e)) from e

    # 断点续采：先找候选（沿用其 ts/db/journal），找不到回落正常新建
    _check_cancel()  # 中断落在目标目录创建之前 → 不留任何现场
    resumed = False
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if resume:
        cand = _find_resume_candidate(Path(data_root) / volume_id)
        if cand is not None:
            dest_dir = cand
            db_path = cand / "snapshot.db"
            ts = cand.name
            resumed = True
    sid = f"{volume_id}/{ts}"
    if not resumed:
        dest_dir = snapshot_path(data_root, sid).parent
        db_path = dest_dir / "snapshot.db"

    # 同 volume_id 下先清扫其他未 sealed 的残留目录（可续采的自动保留，
    # keep_ts 罩住本次目标），再处理本次目标
    _sweep_leftovers(Path(data_root), volume_id, ts, warnings)
    if not resumed and db_path.exists():
        if _is_sealed(db_path):
            raise CollectError(
                f"目标快照已存在且已封库：{sid}（同秒重跑或重复采集；如需重建请先删除 {dest_dir}）")
        # 未封库 = 上次采集崩溃的脏残留（无 journal，不可续采），一律删除重建
        rmtree_ro(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    journal: "ScanJournal | None" = None
    completed: "set[str] | None" = None

    def _cleanup_cancelled() -> None:
        """中断（取消/失败）不丢弃：目标目录与 scan_journal 保留供续采；
        只清理盘上副本临时目录。"""
        if journal is not None:
            try:
                journal.close()  # 落盘已记完成目录（先 commit DB，保证不变量）
            except Exception:  # noqa: BLE001 — 中断清理不掩盖原异常
                _log.warning("中断时 journal 落盘失败（%s）", journal._path)
        try:
            rmtree_ro(Path(scan_root) / "_coldmanifest" / volume_id / ts)
        except OSError as exc:
            _log.warning("中断清理失败（残留 %s）：%s",
                         Path(scan_root) / "_coldmanifest" / volume_id / ts, exc)

    try:
        _check_cancel()
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            if resumed:
                # 续采：沿用首跑建的库。OR IGNORE 依赖 path 唯一索引——首跑
                # 封库前没有索引，此处一次性补建（其后一直保留，seal 阶段幂等）
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_entries_path ON entries(path)")
                conn.commit()
                completed = read_completed(dest_dir) or set()
                # 首跑已写全量 meta；resume_state 补写兜底（异常中断在写 meta 前）
                conn.execute("INSERT OR REPLACE INTO meta(key, value)"
                             " VALUES('resume_state', 'incomplete')")
                conn.commit()
                writer = _SQLiteEntryWriter(conn, resume=True)
                journal = ScanJournal(dest_dir, on_flush=conn.commit)
            else:
                conn.executescript(SNAPSHOT_TABLES_DDL)
                conn.execute("BEGIN")

                _write_meta(conn, scan_root, volume_id, vol, disk, ts,
                            exclude_globs, exclude_hidden, include_system,
                            cross_filesystems=cross_filesystems,
                            serial_fallback=serial_fallback,
                            probe_serial_raw=probe_serial_raw)
                # 断点续采标记：创建即 'incomplete'，封库成功后置 'complete'
                conn.execute("INSERT OR REPLACE INTO meta(key, value)"
                             " VALUES('resume_state', 'incomplete')")
                writer = _SQLiteEntryWriter(conn)
                journal = ScanJournal(dest_dir, on_flush=conn.commit)
            if long_path_warning:
                # skipped 表留痕（与 warnings 列表同内容，快照内可查）
                conn.execute(
                    "INSERT OR IGNORE INTO skipped(path, warning_type, stage, detail)"
                    " VALUES(?, 'long_path', 'probe', ?)",
                    (str(scan_root),
                     "扫描根路径超过 200 字符：建议启用 Windows 长路径支持"
                     "（LongPathsEnabled），否则深层文件可能因 MAX_PATH 采集失败"))
            _check_cancel()

            def _journal_record(rel: str) -> None:
                # 目录子树完成：skipped 缓冲同批提交，再记 journal（顺序不可换）
                writer._flush_skipped()
                assert journal is not None
                journal.record(rel)

            scan_stats = scan_tree(
                scan_root, writer,
                exclude_globs=exclude_globs, exclude_hidden=exclude_hidden,
                include_system=include_system,
                cancel_event=cancel_event,
                progress_cb=(lambda done, _bytes: progress_cb("scan", done, None))
                if progress_cb is not None else None,
                journal_cb=_journal_record,
                skip_subtrees=completed,
                one_filesystem=not cross_filesystems,
            )
            if journal is not None:
                journal.close()
                journal = None
            writer._flush_skipped()
            skipped_count = conn.execute("SELECT COUNT(*) FROM skipped").fetchone()[0]

            _check_cancel()
            if progress_cb is not None:
                progress_cb("seal", 0, 1)
            try:
                # seal 内部步骤粒度（index 1 步 + rollup 按 depth 多步 + optimize 1 步）
                seal_snapshot(conn, progress=(lambda d, t: progress_cb("seal", d, t))
                              if progress_cb is not None else None)
            except sqlite3.Error as e:
                raise CollectError(f"封库失败：{e}") from e
            # status=sealed 在封库成功后写入：seal 中途失败不应留下 sealed 标记；
            # resume_state 同批置 complete（保留键：状态轨迹可诊断）
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('status', 'sealed')")
            conn.execute("INSERT OR REPLACE INTO meta(key, value)"
                         " VALUES('resume_state', 'complete')")
            conn.commit()
            if progress_cb is not None:
                progress_cb("seal", 1, 1)

            stats = {
                "file_count": conn.execute(
                    "SELECT COUNT(*) FROM entries WHERE type='file'").fetchone()[0],
                "dir_count": conn.execute(
                    "SELECT COUNT(*) FROM entries WHERE type='dir' AND entry_id != 1").fetchone()[0],
                "total_bytes": conn.execute(
                    "SELECT COALESCE(SUM(size_bytes),0) FROM entries WHERE type='file'").fetchone()[0],
                "total_alloc": conn.execute(
                    "SELECT COALESCE(SUM(allocated_bytes),0) FROM entries WHERE type='file'").fetchone()[0],
                "zero_byte_count": conn.execute(
                    "SELECT COUNT(*) FROM entries WHERE type='file' AND size_bytes=0").fetchone()[0],
                "max_depth": conn.execute(
                    "SELECT COALESCE(MAX(depth),0) FROM entries").fetchone()[0],
                "skipped_count": skipped_count,
            }
        finally:
            conn.close()

        _check_cancel()
        host_sha = _sha256_file(db_path)

        on_disk_path: Path | None = None
        on_disk_sha: str | None = None
        if on_disk_copy:
            copy_total = db_path.stat().st_size
            if progress_cb is not None:
                progress_cb("copy", 0, copy_total)
            on_disk_path, on_disk_sha = _copy_to_disk(
                scan_root, volume_id, ts, db_path, stats, warnings, host_sha256=host_sha,
                cancel_event=cancel_event,
                copy_progress=(lambda d, t: progress_cb("copy", d, t))
                if progress_cb is not None else None)
            if progress_cb is not None:
                progress_cb("copy", copy_total, copy_total)

        _check_cancel()
        if progress_cb is not None:
            progress_cb("register", 0, 1)
        collected_at = datetime.strptime(ts, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
        _register_catalog(data_root, sid, volume_id, vol, disk, stats,
                          exclude_globs, exclude_hidden, on_disk_copy,
                          on_disk_path, on_disk_sha, collected_at, warnings,
                          root_path=scan_root,
                          cross_filesystems=cross_filesystems)
        if progress_cb is not None:
            progress_cb("register", 1, 1)
            progress_cb("done", 1, 1)
    except CollectCancelled:
        _cleanup_cancelled()
        raise
    except ScanCancelled:
        _cleanup_cancelled()
        raise CollectCancelled("采集已取消（scan 阶段）") from None
    except CollectError:
        _cleanup_cancelled()  # 失败同样保留现场供 --resume
        raise
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as e:
        # 统一兜底：扫描/封库/统计阶段的底层错误 → CollectError（CLI 退出码 2）
        _cleanup_cancelled()
        raise CollectError(f"采集失败：{e}") from e

    return CollectResult(
        snapshot_id=sid, volume_id=volume_id, db_path=db_path,
        files=stats["file_count"], dirs=stats["dir_count"],
        symlinks=scan_stats.symlinks, others=scan_stats.others,
        total_bytes=stats["total_bytes"], total_allocated=stats["total_alloc"],
        skipped=stats["skipped_count"], elapsed_s=time.monotonic() - t0,
        host_sha256=host_sha, on_disk_path=on_disk_path, on_disk_sha256=on_disk_sha,
        resumed=resumed, warnings=warnings,
    )
