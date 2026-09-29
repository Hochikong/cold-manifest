"""catalog.db 注册表操作：磁盘 / 卷 / 批次 / 快照登记（§5.2）。"""

import re
import sqlite3
from pathlib import Path
from typing import Any

from .db import init_catalog, open_catalog

# volume_id 出现在文件路径中，禁止路径分隔符与 ..
_VOLUME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


def validate_volume_id(volume_id: str) -> None:
    """volume_id 必须匹配 ^[A-Za-z0-9][A-Za-z0-9_.\\-]*$（拒绝 /、\\、.. 等）。"""
    from .import_legacy import LegacyImportError

    if not _VOLUME_ID_RE.match(volume_id or ""):
        raise LegacyImportError(f"非法 volume_id：{volume_id!r}（允许字母数字开头的 [A-Za-z0-9_.-]）")


def catalog_path(data_root: "str | Path") -> Path:
    """catalog.db 的标准位置：<data_root>/catalog.db（data_root 即 data 目录）。"""
    return Path(data_root) / "catalog.db"


def connect_catalog(data_root: "str | Path", check_same_thread: bool = True) -> sqlite3.Connection:
    """打开（必要时创建）data 目录与 catalog.db。

    check_same_thread=False 供 Web API 使用：端点在线程池执行，连接跨线程共享
    （catalog 查询本身串行于 FastAPI 的每请求线程，SQLite 层加锁保证安全）。
    """
    conn = open_catalog(catalog_path(data_root), check_same_thread=check_same_thread)
    init_catalog(conn)
    return conn


def ensure_disk(conn: sqlite3.Connection, disk_id: str, **fields: Any) -> None:
    """登记磁盘（幂等）：已存在则只刷新 last_seen，其余字段不动。"""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cols = {"disk_id": disk_id, "first_seen": now, "last_seen": now, **fields}
    keys = list(cols)
    updates = "last_seen=excluded.last_seen, " + ", ".join(
        f"{k}=COALESCE(excluded.{k}, {k})" for k in keys if k not in ("disk_id", "first_seen", "last_seen"))
    conn.execute(
        f"INSERT INTO disks({','.join(keys)}) VALUES({','.join('?' * len(keys))}) "
        f"ON CONFLICT(disk_id) DO UPDATE SET {updates}",
        [cols[k] for k in keys],
    )


def ensure_volume(conn: sqlite3.Connection, volume_id: str, disk_id: str, **fields: Any) -> None:
    """登记卷（幂等）：已存在则保留已有真值、回填缺失字段（如 partition_uuid）。"""
    cols = {"volume_id": volume_id, "disk_id": disk_id, **fields}
    keys = list(cols)
    updates = ", ".join(f"{k}=COALESCE(excluded.{k}, {k})" for k in keys if k != "volume_id")
    conn.execute(
        f"INSERT INTO volumes({','.join(keys)}) VALUES({','.join('?' * len(keys))}) "
        f"ON CONFLICT(volume_id) DO UPDATE SET {updates}",
        [cols[k] for k in keys],
    )


def create_batch(conn: sqlite3.Connection, disk_id: str, planned_volumes: "list[str] | None" = None) -> str:
    """登记一个导入/采集批次。导入场景直接以 done 状态登记。"""
    import json
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    batch_id = f"imp_{now[0:4]}{now[5:7]}{now[8:10]}T{now[11:13]}{now[14:16]}{now[17:19]}Z"
    conn.execute(
        "INSERT OR IGNORE INTO batches(batch_id, disk_id, started_at, finished_at, status, planned_volumes_json)"
        " VALUES(?,?,?,?,?,?)",
        (batch_id, disk_id, now, now, "done", json.dumps(planned_volumes or [])),
    )
    return batch_id


def register_snapshot(conn: sqlite3.Connection, snapshot_id: str, volume_id: str, **stats: Any) -> None:
    """登记/更新快照记录（统计字段经列名白名单过滤）。"""
    allowed = {
        "batch_id", "collected_at", "collector_version", "host_path", "status",
        "hash_policy", "exclude_rules_json", "file_count", "dir_count",
        "total_bytes", "total_alloc", "zero_byte_count", "max_depth",
        "skipped_count", "smart_summary_json", "notes",
    }
    cols = {"snapshot_id": snapshot_id, "volume_id": volume_id,
            **{k: v for k, v in stats.items() if k in allowed}}
    keys = list(cols)
    updates = ", ".join(f"{k}=excluded.{k}" for k in keys if k != "snapshot_id")
    conn.execute(
        f"INSERT INTO snapshots({','.join(keys)}) VALUES({','.join('?' * len(keys))}) "
        f"ON CONFLICT(snapshot_id) DO UPDATE SET {updates}",
        [cols[k] for k in keys],
    )


def find_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> "sqlite3.Row | None":
    """按 snapshot_id 查快照记录。"""
    cur = conn.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,))
    return cur.fetchone()


def snapshot_path(data_root: "str | Path", snapshot_id: str) -> Path:
    """快照库文件路径：data/<volume_id>/<ts>/snapshot.db。

    snapshot_id 形如 ``<volume_id>/<ts>``（volume_id 内不允许出现 '/'）。
    """
    volume_id = snapshot_id.split("/", 1)[0]
    validate_volume_id(volume_id)
    return Path(data_root) / snapshot_id / "snapshot.db"
