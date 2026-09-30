"""就地登记 snapshot.db 到 catalog（P4-⑧ import-db）。

场景：catalog 行缺失（换机/重建/手工拷入）但快照库还在
``<data_root>/<volume_id>/<ts>/snapshot.db``（或任意位置的库文件）。
读库内 meta（volume_id / disk / 采集口径 / 时间等）→ ensure_disk /
ensure_volume / register_snapshot；**仅就地登记，不拷贝任何文件**。

- 幂等：已登记 → 更新快照字段（register_snapshot 全字段覆盖）并提示；
- host_path 取 meta 的 scan_root/root_path；缺失 → warning 但仍登记
  （依赖 host_path 的功能——按需哈希、副本校验——会提示不可用）；
- 库不存在 / 未封库 → ImportDbError（CLI 转退出码 2）；
- 盘上副本行：scan_root 之下 ``_coldmanifest/<vol>/<ts>/snapshot.db``
  真实存在时补记（含 sha256），否则跳过。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .catalog import (connect_catalog, create_batch, ensure_disk,
                      ensure_volume, find_snapshot, register_snapshot,
                      validate_volume_id)
from .rebuild import (derive_disk_id, read_snapshot_meta, read_snapshot_stats)
from .seal import is_sealed

_TS_RE = re.compile(r"^\d{8}T\d{6}Z$")


class ImportDbError(Exception):
    """库不存在 / 未封库 / 路径无法推导快照 ID（CLI 转退出码 2）。"""


def _int_meta(meta: dict, key: str) -> "int | None":
    v = meta.get(key)
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def import_snapshot_db(db_path: "str | Path", data_root: "str | Path") -> dict:
    """把一个已封库的 snapshot.db 就地登记进 data_root 的 catalog。"""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise ImportDbError(f"快照库不存在：{db_path}")
    if not is_sealed(db_path):
        raise ImportDbError(f"该库未封库（meta.status != sealed）：{db_path}")

    try:
        meta = read_snapshot_meta(db_path)
        stats = read_snapshot_stats(db_path)
    except sqlite3.Error as e:
        raise ImportDbError(f"快照库损坏，无法读取：{db_path}（{e}）") from e

    ts = db_path.parent.name
    volume_id = (meta.get("volume_id") or db_path.parent.parent.name).strip()
    if not _TS_RE.match(ts):
        raise ImportDbError(
            f"无法推导采集时间戳：库须位于 <volume_id>/<ts>/snapshot.db 布局下"
            f"（当前父目录：{ts!r}）")
    try:
        validate_volume_id(volume_id)
    except Exception as e:
        raise ImportDbError(str(e)) from e

    snapshot_id = f"{volume_id}/{ts}"
    disk_id = derive_disk_id(volume_id, meta)
    warnings: "list[str]" = []

    host_path = meta.get("scan_root") or meta.get("root_path") or ""
    if not host_path:
        warnings.append(
            "meta 缺少 scan_root/root_path：host_path 记为空，"
            "按需哈希/盘上副本校验等依赖源路径的功能不可用")

    collected_at = meta.get("collect_time_utc")
    if not collected_at:
        collected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    cat = connect_catalog(data_root)
    try:
        ensure_disk(cat, disk_id,
                    physical_model=meta.get("physical_model") or None,
                    physical_serial=meta.get("physical_serial") or None,
                    bridge_model=meta.get("bridge_model") or None,
                    capacity_bytes=_int_meta(meta, "disk_capacity_bytes"),
                    interface_type=meta.get("interface_type") or None)
        ensure_volume(cat, volume_id, disk_id,
                      partition_index=_int_meta(meta, "partition_index"),
                      partition_uuid=meta.get("partition_uuid") or None,
                      volume_serial_hex=meta.get("volume_serial_hex") or None,
                      filesystem=meta.get("filesystem") or None,
                      label=meta.get("volume_label") or None,
                      capacity_bytes=_int_meta(meta, "partition_capacity_bytes"))

        existing = find_snapshot(cat, snapshot_id)
        created = existing is None
        batch_id = existing["batch_id"] if existing else \
            create_batch(cat, disk_id, [volume_id])
        register_snapshot(cat, snapshot_id, volume_id,
                          batch_id=batch_id,
                          collected_at=collected_at,
                          collector_version=meta.get("collector_version") or None,
                          host_path=host_path,
                          status="sealed",
                          hash_policy=meta.get("hash_policy") or "none",
                          exclude_rules_json=meta.get("exclude_rules_json") or None,
                          file_count=stats["file_count"],
                          dir_count=stats["dir_count"],
                          total_bytes=stats["total_bytes"],
                          total_alloc=stats["total_alloc"],
                          zero_byte_count=stats["zero_byte_count"],
                          max_depth=stats["max_depth"],
                          skipped_count=stats["skipped_count"])

        if host_path:
            copy_db = Path(host_path) / "_coldmanifest" / volume_id / ts / "snapshot.db"
            if copy_db.is_file():
                from .collect import _sha256_file
                cat.execute(
                    "INSERT INTO on_disk_copies(snapshot_id, disk_path, copied_at,"
                    " sha256, verified_at, status) VALUES(?,?,?,?,?,?)"
                    " ON CONFLICT(snapshot_id) DO UPDATE SET"
                    " disk_path=excluded.disk_path, copied_at=excluded.copied_at,"
                    " sha256=excluded.sha256, verified_at=excluded.verified_at,"
                    " status=excluded.status",
                    (snapshot_id, str(copy_db), collected_at,
                     _sha256_file(copy_db), collected_at, "ok"))
            else:
                warnings.append(
                    f"盘上副本未找到（未登记副本行）：{copy_db}")
        cat.commit()
    finally:
        cat.close()

    return {
        "snapshot_id": snapshot_id,
        "volume_id": volume_id,
        "db_path": db_path,
        "created": created,
        "warnings": warnings,
    }
