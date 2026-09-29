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
                      register_snapshot, snapshot_path, validate_volume_id)
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
                mtime_ns, ctime_ns, btime_ns, attrs, ext, path.casefold(), error)
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
            shutil.rmtree(child)
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
    """catalog 注册：disk/volume/batch/snapshot + on_disk_copies。返回 batch_id。"""
    serial = (disk.disk_serial or "").strip()
    disk_id = serial or f"NOSERIAL_{volume_id}"
    cat = connect_catalog(data_root)
    try:
        ensure_disk(cat, disk_id,
                    physical_model=disk.physical_model or None,
                    physical_serial=disk.physical_serial or None,
                    bridge_model=disk.bridge_model or None,
                    capacity_bytes=disk.capacity_bytes,
                    interface_type=disk.interface_type or None)
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
        cat.commit()
    finally:
        cat.close()
    return batch_id


def _usable_serial(serial: "str | None") -> bool:
    """探测序列号是否可用。

    视为不可用：空串、全 0 / 全占位符、清洗为 volume_id 字符集后不合法
    （首字符非字母数字）的值——廉价 USB 桥常返回 "0"、"0000000" 甚至带
    控制字节的残串（真盘 F: 实测 '\\x030'），这些值直接拼 volume_id 必然
    被 validate_volume_id 拒绝，应走卷序列号回退或显式 --serial。
    """
    s = (serial or "").strip()
    if not s:
        return False
    # 按 volume_id 的清洗口径检查（控制字符/空格等替换为 _ 后再判）：
    # 首字符须为字母数字（validate_volume_id 同口径），且不能是全 0 占位
    sanitized = re.sub(r"[^A-Za-z0-9_.\-]", "_", s)
    if not re.match(r"[A-Za-z0-9]", sanitized):
        return False
    return any(c != "0" for c in sanitized)


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

    serial_fallback = False
    probe_serial_raw = ""
    if volume_id is None:
        serial = (disk.disk_serial or "").strip()
        index = vol.partition_index if vol.partition_index is not None else 0
        if not _usable_serial(serial):
            # 占位序列号（空/"0"/全 0，USB 桥常见）→ 回退卷序列号 hex 命名
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
            else:
                raise CollectError(
                    "未能读取盘序列号（USB 桥常见），请显式指定 `--serial <值>`"
                    "（或用 --volume-id 手动命名该卷）")
        else:
            # 怪异序列号（空格/斜杠/Unicode 等）清洗为合法 volume_id 字符，再统一校验
            serial = re.sub(r"[^A-Za-z0-9_.\-]", "_", serial)
            volume_id = f"{serial}_P{index}"
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
        shutil.rmtree(dest_dir)
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
            shutil.rmtree(Path(scan_root) / "_coldmanifest" / volume_id / ts)
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
