"""在线采集编排（§4.3）：probe → scan → seal → on-disk copy → catalog 注册。

引擎与 CLI ``cldm collect``、Web 采集任务共用同一入口 ``collect_volume``。
快照库自包含（meta 自描述采集口径），封库逻辑与 import_legacy 共用 seal.py。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .catalog import (connect_catalog, create_batch, ensure_disk, ensure_volume,
                      register_snapshot, snapshot_path, validate_volume_id)
from .probe import DiskInfo, ProbeError, VolumeInfo, probe_path
from .scanner import scan_tree
from .schema import SNAPSHOT_TABLES_DDL
from .seal import seal_snapshot

# 盘上副本的空间余量：库大小 + 64 MiB（旁车与临时空间）
_COPY_MARGIN_BYTES = 64 * 1024 * 1024
_BATCH_SIZE = 10_000

ProgressCB = Callable[[str, int, "int | None"], None]


class CollectError(Exception):
    """采集失败（probe 失败、非法 volume_id、目标已存在等）。"""


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
    warnings: list[str] = field(default_factory=list)


class _SQLiteEntryWriter:
    """scan_tree → entries/skipped 的批量写入适配器（万行批事务）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._buf: list[tuple] = []
        self._skipped_buf: list[tuple] = []

    def add_entry(self, *, parent_id: int, path: str, name: str, depth: int, type: str,
                  size_bytes: int | None, allocated_bytes: int | None, mtime_ns: int | None,
                  ctime_ns: int | None, btime_ns: int | None, attrs: int | None, ext: str,
                  error: str | None = None) -> int:
        eid = self._conn.execute(
            "INSERT INTO entries(parent_id,path,name,depth,type,size_bytes,allocated_bytes,"
            "mtime_ns,ctime_ns,btime_ns,attrs,ext,path_norm,error)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (parent_id, path, name, depth, type, size_bytes, allocated_bytes,
             mtime_ns, ctime_ns, btime_ns, attrs, ext, path.casefold(), error),
        ).lastrowid
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


def _copy_with_hash(src: Path, dst: Path) -> str:
    """流式拷贝 src → dst，同时计算 sha256（单遍读）。"""
    h = hashlib.sha256()
    with src.open("rb") as fin, dst.open("wb") as fout:
        while True:
            block = fin.read(4 * 1024 * 1024)
            if not block:
                break
            h.update(block)
            fout.write(block)
    return h.hexdigest()


def _write_meta(conn: sqlite3.Connection, scan_root: Path, volume_id: str,
                vol: VolumeInfo, disk: DiskInfo, ts: str,
                exclude_globs: list[str] | None, exclude_hidden: bool,
                include_system: bool) -> None:
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
        "scan_root": str(Path(scan_root).resolve()),
        "exclude_rules_json": json.dumps({
            "exclude_globs": list(exclude_globs or []),
            "exclude_hidden": exclude_hidden,
            "include_system": include_system,
        }, ensure_ascii=False),
        "hash_policy": "none",
        "collect_time_utc": datetime.strptime(ts, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ"),
        "collector_version": __version__,
    }
    if smart_raw:
        meta["smart_raw_json"] = smart_raw if isinstance(smart_raw, str) else json.dumps(smart_raw)
    conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                     list(meta.items()))


def _is_sealed(db_path: Path) -> bool:
    """快照库是否已封库（meta 中存在 status=sealed）。与 import_legacy 同口径。"""
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='status'").fetchone()
        return bool(row) and row[0] == "sealed"
    except sqlite3.Error:
        return False


def _sweep_leftovers(data_root: Path, volume_id: str, keep_ts: str,
                     warnings: list[str]) -> None:
    """清扫同 volume_id 下未 sealed 的其他残留目录（上次采集崩溃留下的脏库）。"""
    vol_root = data_root / volume_id
    if not vol_root.is_dir():
        return
    for child in vol_root.iterdir():
        if not child.is_dir() or child.name == keep_ts:
            continue
        db = child / "snapshot.db"
        if db.exists() and _is_sealed(db):
            continue  # 已封库的历史快照，不动
        try:
            shutil.rmtree(child)
        except OSError as exc:
            warnings.append(f"leftover_cleanup=failed({child}: {exc})")
        else:
            warnings.append(f"leftover_cleaned={child.name}")


def _copy_to_disk(scan_root: Path, volume_id: str, ts: str, db_path: Path,
                  stats: dict, warnings: list[str],
                  host_sha256: str) -> tuple[Path | None, str | None]:
    """§4.6 盘上副本：``<scan_root>/_coldmanifest/<volume_id>/<ts>/snapshot.db`` + snapshot.json。

    返回 (副本路径或 None, 副本 sha256 或 None)；空间不足/失败只记 warning，不抛异常。
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
        copy_hash = _copy_with_hash(db_path, tmp)
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
                      collected_at: str, warnings: list[str]) -> str:
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
                          host_path=str(snapshot_path(data_root, snapshot_id)),
                          status="sealed", hash_policy="none",
                          exclude_rules_json=json.dumps({
                              "exclude_globs": list(exclude_globs or []),
                              "exclude_hidden": exclude_hidden,
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


def collect_volume(scan_root: "str | Path", *, data_root: "str | Path",
                   volume_id: str | None = None, manual_serial: str | None = None,
                   exclude_globs: list[str] | None = None, exclude_hidden: bool = False,
                   include_system: bool = True, smartctl: bool = True,
                   on_disk_copy: bool = True,
                   progress_cb: ProgressCB | None = None) -> CollectResult:
    """采集一个卷：probe → scan → seal → 盘上副本 → catalog 注册。

    progress_cb(phase, done, total)，phase ∈ {"probe","scan","seal","copy","register","done"}；
    scan 阶段 total=None（流式，总数未知）。
    """
    t0 = time.monotonic()
    warnings: list[str] = []
    scan_root = Path(scan_root)
    if not scan_root.is_dir():
        raise CollectError(f"扫描根不存在或不是目录：{scan_root}")
    data_root = Path(data_root)

    if progress_cb is not None:
        progress_cb("probe", 0, 1)
    try:
        vol, disk = probe_path(str(scan_root), manual_serial=manual_serial, smartctl=smartctl)
    except ProbeError as e:
        raise CollectError(f"probe 失败：{e}") from e
    if progress_cb is not None:
        progress_cb("probe", 1, 1)

    if volume_id is None:
        serial = (disk.disk_serial or "").strip()
        if not serial:
            raise CollectError("无法确定磁盘序列号（可用 --serial 手动指定或 --volume-id 跳过 probe 命名）")
        index = vol.partition_index if vol.partition_index is not None else 0
        # 怪异序列号（空格/斜杠/Unicode 等）清洗为合法 volume_id 字符，再统一校验
        serial = re.sub(r"[^A-Za-z0-9_.\-]", "_", serial)
        volume_id = f"{serial}_P{index}"
    try:
        validate_volume_id(volume_id)
    except Exception as e:
        # 含 LegacyImportError：CLI 层只认 CollectError（退出码 2，不裸 traceback）
        raise CollectError(str(e)) from e

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sid = f"{volume_id}/{ts}"
    dest_dir = snapshot_path(data_root, sid).parent
    db_path = dest_dir / "snapshot.db"

    # 同 volume_id 下先清扫其他未 sealed 的残留目录，再处理本次目标
    _sweep_leftovers(Path(data_root), volume_id, ts, warnings)
    if db_path.exists():
        if _is_sealed(db_path):
            raise CollectError(
                f"目标快照已存在且已封库：{sid}（同秒重跑或重复采集；如需重建请先删除 {dest_dir}）")
        # 未封库 = 上次采集崩溃的脏残留，一律删除重建
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            conn.executescript(SNAPSHOT_TABLES_DDL)
            conn.execute("BEGIN")

            _write_meta(conn, scan_root, volume_id, vol, disk, ts,
                        exclude_globs, exclude_hidden, include_system)
            writer = _SQLiteEntryWriter(conn)
            scan_stats = scan_tree(
                scan_root, writer,
                exclude_globs=exclude_globs, exclude_hidden=exclude_hidden,
                include_system=include_system,
                progress_cb=(lambda done, _bytes: progress_cb("scan", done, None))
                if progress_cb is not None else None,
            )
            writer._flush_skipped()
            skipped_count = conn.execute("SELECT COUNT(*) FROM skipped").fetchone()[0]

            if progress_cb is not None:
                progress_cb("seal", 0, 3)
            try:
                seal_snapshot(conn)
            except sqlite3.Error as e:
                raise CollectError(f"封库失败：{e}") from e
            # status=sealed 在封库成功后写入：seal 中途失败不应留下 sealed 标记
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('status', 'sealed')")
            conn.commit()
            if progress_cb is not None:
                progress_cb("seal", 3, 3)

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
    except CollectError:
        raise
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as e:
        # 统一兜底：扫描/封库/统计阶段的底层错误 → CollectError（CLI 退出码 2）
        raise CollectError(f"采集失败：{e}") from e
    host_sha = _sha256_file(db_path)

    on_disk_path: Path | None = None
    on_disk_sha: str | None = None
    if on_disk_copy:
        if progress_cb is not None:
            progress_cb("copy", 0, 1)
        on_disk_path, on_disk_sha = _copy_to_disk(
            scan_root, volume_id, ts, db_path, stats, warnings, host_sha256=host_sha)
        if progress_cb is not None:
            progress_cb("copy", 1, 1)

    if progress_cb is not None:
        progress_cb("register", 0, 1)
    collected_at = datetime.strptime(ts, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
    _register_catalog(data_root, sid, volume_id, vol, disk, stats,
                      exclude_globs, exclude_hidden, on_disk_copy,
                      on_disk_path, on_disk_sha, collected_at, warnings)
    if progress_cb is not None:
        progress_cb("register", 1, 1)
        progress_cb("done", 1, 1)

    return CollectResult(
        snapshot_id=sid, volume_id=volume_id, db_path=db_path,
        files=stats["file_count"], dirs=stats["dir_count"],
        symlinks=scan_stats.symlinks, others=scan_stats.others,
        total_bytes=stats["total_bytes"], total_allocated=stats["total_alloc"],
        skipped=stats["skipped_count"], elapsed_s=time.monotonic() - t0,
        host_sha256=host_sha, on_disk_path=on_disk_path, on_disk_sha256=on_disk_sha,
        warnings=warnings,
    )
