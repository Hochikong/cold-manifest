"""catalog 重建（P4-⑥）：扫描 <data_root>/<volume_id>/<ts>/snapshot.db 的 meta，
补建/回填 catalog 的 disks / volumes / snapshots / on_disk_copies 行。

场景：catalog.db 丢失或部分行缺失（换机、误删、旧版本未注册），快照库本身
自包含 meta，可据此重建注册表。原则：

- **不覆盖已有行**：已存在的 disks/volumes 行只把「已有值为 NULL 且新值非空」
  的字段 COALESCE 回填；snapshots 已有行同样只回填 NULL 字段（绝不改已有值）；
- 缺失行按 meta 补建（盘信息缺失时按 volume_id 的 serial 前缀造 disk_id，
  与 collect 命名口径 ``{serial}_P{index}`` / ``NOSERIAL_{volume_id}`` 一致）；
- 幂等：重复运行 added=0；diff_runs/tasks/hash_cache 等一概不动；
- 损坏（打不开）或未封库（meta.status != sealed）的库跳过并计入 warnings；
- dry_run 只输出将补/将修的行数，不写任何东西。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import (connect_catalog, create_batch, validate_volume_id)
from .seal import is_sealed


class RebuildError(Exception):
    """rebuild 参数/环境错误（CLI 转退出码 2）。"""


_TS_RE = re.compile(r"^\d{8}T\d{6}Z$")


def read_snapshot_meta(db_path: "str | Path") -> dict:
    """只读读取快照库 meta 键值；库打不开/无 meta 表 → sqlite3.Error。"""
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
    finally:
        conn.close()


def read_snapshot_stats(db_path: "str | Path") -> dict:
    """从快照库实数统计（与 collect 封库期统计同口径）。"""
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        return {
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
            "skipped_count": conn.execute(
                "SELECT COUNT(*) FROM skipped").fetchone()[0],
        }
    finally:
        conn.close()


def _int_meta(meta: dict, key: str) -> "int | None":
    v = meta.get(key)
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def derive_disk_id(volume_id: str, meta: dict) -> str:
    """disk_id 命名与 collect 一致：serial 或 volume_id 的 serial 前缀。

    collect：volume_id = ``{serial}_P{index}``，disk_id = serial
    （serial 不可用时 ``NOSERIAL_{volume_id}``，此时 volume_id 形如
    ``VOL-{hex}_P{index}``，前缀本身就是稳定锚）。重建时盘信息缺失，
    剥掉尾部 ``_P{index}`` 得 serial 前缀。
    """
    serial = (meta.get("disk_serial") or "").strip()
    if serial:
        return serial
    prefix = re.sub(r"_P\d+$", "", volume_id)
    return prefix or f"NOSERIAL_{volume_id}"


def _backfill_count(existing: Any, fields: dict) -> int:
    """已有行中「现值为 NULL 且新值非 None」的字段数（回填量）。"""
    if existing is None:
        return 0
    n = 0
    for k, v in fields.items():
        if v is not None and existing[k] is None:
            n += 1
    return n


def _apply_backfill(cat: sqlite3.Connection, table: str, key_col: str,
                    key_val: str, fields: dict) -> None:
    """COALESCE 回填：已有值在前（绝不覆盖），只填 NULL。"""
    items = [(k, v) for k, v in fields.items() if v is not None]
    if not items:
        return
    sets = ", ".join(f"{k}=COALESCE({k}, ?)" for k, _ in items)
    cat.execute(f"UPDATE {table} SET {sets} WHERE {key_col}=?",
                [v for _, v in items] + [key_val])


def rebuild_catalog(data_root: "str | Path", *, dry_run: bool = False) -> dict:
    """扫描 data/*/*/snapshot.db，补建/回填 catalog 注册行。返回摘要计数。"""
    data_root = Path(data_root)
    if not data_root.is_dir():
        raise RebuildError(f"数据根不存在或不是目录：{data_root}")

    result: dict = {
        "scanned": 0,
        "disks_added": 0,
        "volumes_added": 0,
        "snapshots_added": 0,
        "copies_added": 0,
        "fields_backfilled": 0,
        "snapshots_skipped": 0,
        "warnings": [],
    }
    warnings = result["warnings"]

    cat = connect_catalog(data_root)
    try:
        for db_path in sorted(data_root.glob("*/*/snapshot.db")):
            volume_id = db_path.parent.parent.name
            ts = db_path.parent.name
            try:
                validate_volume_id(volume_id)
            except Exception as e:  # noqa: BLE001 — 非法目录名（含 ..）不进入重建
                warnings.append(f"跳过非法卷目录 {db_path.parent}：{e}")
                continue
            if not _TS_RE.match(ts):
                warnings.append(f"跳过非时间戳目录：{db_path.parent}")
                continue
            try:
                meta = read_snapshot_meta(db_path)
            except sqlite3.Error as e:
                warnings.append(f"损坏，跳过：{db_path}（{e}）")
                continue
            if meta.get("status") != "sealed":
                warnings.append(f"未封库，跳过：{db_path}")
                continue
            try:
                stats = read_snapshot_stats(db_path)
            except sqlite3.Error as e:
                warnings.append(f"损坏，跳过：{db_path}（{e}）")
                continue
            result["scanned"] += 1
            _reconcile_snapshot(cat, result, warnings, dry_run,
                                volume_id, ts, db_path, meta, stats)
        if not dry_run:
            cat.commit()
    finally:
        cat.close()
    return result


def _reconcile_snapshot(cat: sqlite3.Connection, result: dict,
                        warnings: "list[str]", dry_run: bool,
                        volume_id: str, ts: str, db_path: Path,
                        meta: dict, stats: dict) -> None:
    """对单个快照做 补建/回填/跳过 三态对账。"""
    disk_id = derive_disk_id(volume_id, meta)

    disk_fields = {
        "physical_model": meta.get("physical_model") or None,
        "physical_serial": meta.get("physical_serial") or None,
        "bridge_model": meta.get("bridge_model") or None,
        "capacity_bytes": _int_meta(meta, "disk_capacity_bytes"),
        "interface_type": meta.get("interface_type") or None,
    }
    vol_fields = {
        "partition_index": _int_meta(meta, "partition_index"),
        "partition_uuid": meta.get("partition_uuid") or None,
        "volume_serial_hex": meta.get("volume_serial_hex") or None,
        "filesystem": meta.get("filesystem") or None,
        "label": meta.get("volume_label") or None,
        "capacity_bytes": _int_meta(meta, "partition_capacity_bytes"),
    }

    # ---- disks ----
    row = cat.execute("SELECT * FROM disks WHERE disk_id=?", (disk_id,)).fetchone()
    if row is None:
        result["disks_added"] += 1
        if not dry_run:
            from .catalog import ensure_disk
            ensure_disk(cat, disk_id, **disk_fields)
    else:
        result["fields_backfilled"] += _backfill_count(row, disk_fields)
        if not dry_run:
            _apply_backfill(cat, "disks", "disk_id", disk_id, disk_fields)

    # ---- volumes ----
    row = cat.execute("SELECT * FROM volumes WHERE volume_id=?", (volume_id,)).fetchone()
    if row is None:
        result["volumes_added"] += 1
        if not dry_run:
            from .catalog import ensure_volume
            ensure_volume(cat, volume_id, disk_id, **vol_fields)
    else:
        # 已有行不覆盖（含 disk_id 归属——换线登记的归属以既有记录为准）
        result["fields_backfilled"] += _backfill_count(row, vol_fields)
        if not dry_run:
            _apply_backfill(cat, "volumes", "volume_id", volume_id, vol_fields)

    # ---- snapshots ----
    snapshot_id = f"{volume_id}/{ts}"
    collected_at = meta.get("collect_time_utc")
    if not collected_at:
        try:
            collected_at = datetime.strptime(ts, "%Y%m%dT%H%M%SZ")\
                .strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            collected_at = None
    host_path = meta.get("scan_root") or meta.get("root_path") or ""
    snap_fields = {
        "collected_at": collected_at,
        "collector_version": meta.get("collector_version") or None,
        "host_path": host_path or None,
        "status": "sealed",
        "hash_policy": meta.get("hash_policy") or "none",
        "exclude_rules_json": meta.get("exclude_rules_json") or None,
        "file_count": stats["file_count"],
        "dir_count": stats["dir_count"],
        "total_bytes": stats["total_bytes"],
        "total_alloc": stats["total_alloc"],
        "zero_byte_count": stats["zero_byte_count"],
        "max_depth": stats["max_depth"],
        "skipped_count": stats["skipped_count"],
    }
    row = cat.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    if row is None:
        result["snapshots_added"] += 1
        if not dry_run:
            from .catalog import register_snapshot
            batch_id = create_batch(cat, disk_id, [volume_id])
            register_snapshot(cat, snapshot_id, volume_id,
                              batch_id=batch_id, **snap_fields)
        _reconcile_copy(cat, result, dry_run, volume_id, ts,
                        snapshot_id, meta, collected_at)
    else:
        n = _backfill_count(row, snap_fields)
        if n:
            result["fields_backfilled"] += n
            if not dry_run:
                _apply_backfill(cat, "snapshots", "snapshot_id", snapshot_id,
                                snap_fields)
        else:
            result["snapshots_skipped"] += 1


def _reconcile_copy(cat: sqlite3.Connection, result: dict, dry_run: bool,
                    volume_id: str, ts: str, snapshot_id: str,
                    meta: dict, collected_at: "str | None") -> None:
    """补建 on_disk_copies 行：仅在快照行新建且盘上副本文件真实存在时。"""
    scan_root = meta.get("scan_root") or meta.get("root_path")
    if not scan_root:
        return
    copy_db = Path(scan_root) / "_coldmanifest" / volume_id / ts / "snapshot.db"
    if not copy_db.is_file():
        return
    from .collect import _sha256_file
    result["copies_added"] += 1
    if dry_run:
        return
    cat.execute(
        "INSERT INTO on_disk_copies(snapshot_id, disk_path, copied_at, sha256,"
        " verified_at, status) VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(snapshot_id) DO UPDATE SET disk_path=excluded.disk_path,"
        " copied_at=excluded.copied_at, sha256=excluded.sha256,"
        " verified_at=excluded.verified_at, status=excluded.status",
        (snapshot_id, str(copy_db), collected_at, _sha256_file(copy_db),
         collected_at, "ok"))
