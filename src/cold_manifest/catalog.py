"""catalog.db 注册表操作：磁盘 / 卷 / 批次 / 快照登记（§5.2）。"""

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from .db import file_uri, init_catalog, open_catalog

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
    updates = ("last_seen=excluded.last_seen, " + ", ".join(
        f"{k}=COALESCE(excluded.{k}, {k})"
        for k in keys if k not in ("disk_id", "first_seen", "last_seen"))).rstrip(", ")
    if not updates:
        # 只有 disk_id/first_seen/last_seen（无附加字段）：UPSERT 只刷 last_seen
        updates = "last_seen=excluded.last_seen"
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


class SnapshotDeleteError(Exception):
    """快照删除参数/路径非法（路由层转 400）。"""


class SnapshotDeleteBlocked(Exception):
    """删除被阻塞：被 diff 引用或存在活跃任务（路由层转 409）。

    diffs/tasks：阻塞来源 ID 列表（二选一非空）。
    """

    def __init__(self, message: str, *, diffs: "list[str] | None" = None,
                 tasks: "list[str] | None" = None) -> None:
        super().__init__(message)
        self.diffs = diffs or []
        self.tasks = tasks or []


def _dir_size(path: Path) -> int:
    """递归求目录字节占用（删除前统计 freed_bytes 用；异常按 0 计）。"""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _safe_child(parent: Path, child: Path, what: str) -> None:
    """断言 child 是 parent 的直接子目录（resolve 后比较，防路径穿越）。"""
    parent_r = parent.resolve()
    child_r = child.resolve()
    if child_r.parent != parent_r:
        raise SnapshotDeleteError(f"{what} 路径越界：{child} 不在 {parent_r} 之下")


def delete_snapshot(conn: sqlite3.Connection, data_root: "str | Path",
                    snapshot_id: str, *, on_disk: str = "keep",
                    force: bool = False) -> dict:
    """删除快照：主机快照目录 +（可选）盘上副本 + catalog 注册行。

    - catalog 只删 snapshots / on_disk_copies 行（不动 disks/volumes/hash_cache）；
    - 被 diff_runs 引用 → 默认抛 SnapshotDeleteBlocked（force=True 级联删
      物化 diff 库与 diff_runs 行）；
    - 有活跃任务（pending/running 且 payload 指向该快照或卷）→ 抛 SnapshotDeleteBlocked；
    - 快照不存在 → 抛 LookupError（路由/CLI 转 404）；
    - 调用方须持 DataRootLock（本函数不重复拿锁）。

    返回 {snapshot_id, deleted_host, deleted_disk, freed_bytes,
          diffs_removed, warnings}。
    """
    import shutil

    if on_disk not in ("keep", "delete"):
        raise SnapshotDeleteError(f"非法 on_disk：{on_disk!r}（允许 keep/delete）")

    data_root = Path(data_root)
    volume_id, _, ts = snapshot_id.partition("/")
    # 局部导入避免 catalog ⇄ import_legacy 循环依赖（与 validate_volume_id 同）
    from .import_legacy import LegacyImportError
    try:
        validate_volume_id(volume_id)
    except LegacyImportError as e:  # 非法 volume_id（含 ..、非法字符）→ 400 而非 500
        raise SnapshotDeleteError(str(e)) from None
    if not ts:
        raise SnapshotDeleteError(f"非法 snapshot_id：{snapshot_id!r}（应为 <volume_id>/<ts>）")

    row = find_snapshot(conn, snapshot_id)
    if row is None:
        raise LookupError(f"快照不存在：{snapshot_id}")

    warnings: "list[str]" = []
    # 快照库 meta（scan_root/root_path）要在删主机目录前读
    meta_scan_root = _read_snapshot_meta_scan_root(data_root, snapshot_id)

    # ---- 活跃任务检查（collect 指向卷、hash 指向快照）----
    task_rows = conn.execute(
        "SELECT task_id, kind FROM tasks WHERE status IN ('pending','running')"
        " AND (payload_json LIKE ? OR payload_json LIKE ?)",
        (f'%"{snapshot_id}"%', f'%"{volume_id}"%'),
    ).fetchall()
    if task_rows:
        ids = [r["task_id"] for r in task_rows]
        kinds = ", ".join(sorted({r["kind"] for r in task_rows}))
        raise SnapshotDeleteBlocked(
            f"快照有活跃任务（{kinds}），请等待完成或取消后再删除",
            tasks=ids)

    # ---- diff 引用检查 ----
    ref_rows = conn.execute(
        "SELECT diff_id FROM diff_runs WHERE a=? OR b=?", (snapshot_id, snapshot_id)
    ).fetchall()
    ref_ids = [r["diff_id"] for r in ref_rows]
    if ref_ids and not force:
        raise SnapshotDeleteBlocked(
            f"快照被 {len(ref_ids)} 个 diff 引用：{', '.join(ref_ids)}；"
            f"使用 force 级联删除这些 diff", diffs=ref_ids)

    freed = 0
    diffs_removed: "list[str]" = []

    # ---- 级联删 diff（force）：物化库 + catalog 行 ----
    from .diff_engine import diff_db_path
    for diff_id in ref_ids:
        db = diff_db_path(data_root, diff_id)
        if db.is_file():
            freed += db.stat().st_size
            db.unlink()
        conn.execute("DELETE FROM diff_runs WHERE diff_id=?", (diff_id,))
        diffs_removed.append(diff_id)

    # ---- 主机快照目录 <data_root>/<volume_id>/<ts>/ ----
    host_dir = data_root / volume_id / ts
    _safe_child(data_root / volume_id, host_dir, "主机快照目录")
    deleted_host = False
    if host_dir.is_dir():
        freed += _dir_size(host_dir)
        shutil.rmtree(host_dir)
        deleted_host = True
    else:
        warnings.append(f"主机快照目录不存在（仅清理 catalog 行）：{host_dir}")

    # ---- 盘上副本 <scan_root>/_coldmanifest/<volume_id>/<ts>/ ----
    deleted_disk: "bool | None" = None
    if on_disk == "delete":
        copy_row = conn.execute(
            "SELECT disk_path FROM on_disk_copies WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        disk_path = copy_row["disk_path"] if copy_row else None
        copy_dir, copy_src = _locate_on_disk_dir(conn, meta_scan_root, disk_path,
                                                 volume_id, ts)
        if copy_dir is None:
            deleted_disk = False
            warnings.append(f"盘上副本定位不到（未删除）：{copy_src}")
        else:
            _safe_child(copy_dir.parent, copy_dir, "盘上副本目录")
            if copy_dir.is_dir():
                freed += _dir_size(copy_dir)
                shutil.rmtree(copy_dir)
                deleted_disk = True
            else:
                deleted_disk = False
                warnings.append(f"盘上副本目录不存在（仅清理 catalog 行）：{copy_dir}")

    # ---- catalog 注册行 ----
    conn.execute("DELETE FROM on_disk_copies WHERE snapshot_id=?", (snapshot_id,))
    conn.execute("DELETE FROM snapshots WHERE snapshot_id=?", (snapshot_id,))
    conn.commit()

    return {
        "snapshot_id": snapshot_id,
        "deleted_host": deleted_host,
        "deleted_disk": deleted_disk,
        "freed_bytes": freed,
        "diffs_removed": diffs_removed,
        "warnings": warnings,
    }


def _read_snapshot_meta_scan_root(data_root: Path, snapshot_id: str) -> "str | None":
    """删主机目录前从 snapshot.db 读 meta 的 scan_root / root_path。"""
    db = snapshot_path(data_root, snapshot_id)
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(file_uri(db, immutable=False), uri=True)
    except sqlite3.Error:
        return None
    try:
        for key in ("scan_root", "root_path"):
            r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if r and r[0]:
                return r[0]
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return None


def _locate_on_disk_dir(conn: sqlite3.Connection, meta_scan_root: "str | None",
                        disk_path: "str | None", volume_id: str, ts: str
                        ) -> "tuple[Path | None, str]":
    """定位盘上副本目录。

    优先快照库 meta 的 scan_root/root_path（<scan_root>/_coldmanifest/<vol>/<ts>），
    其次 on_disk_copies.disk_path 的父目录；都不可用返回 (None, 说明)。
    """
    if meta_scan_root:
        scan = Path(meta_scan_root)
        if scan.is_dir():
            return scan / "_coldmanifest" / volume_id / ts, f"{scan}/_coldmanifest/{volume_id}/{ts}"
    if disk_path:
        p = Path(disk_path)
        if p.is_file():
            return p.parent, str(p.parent)
    return None, f"{meta_scan_root or disk_path or '（无 scan_root 记录）'}"


def snapshot_path(data_root: "str | Path", snapshot_id: str) -> Path:
    """快照库文件路径：data/<volume_id>/<ts>/snapshot.db。

    snapshot_id 形如 ``<volume_id>/<ts>``（volume_id 内不允许出现 '/'）。
    """
    volume_id = snapshot_id.split("/", 1)[0]
    validate_volume_id(volume_id)
    return Path(data_root) / snapshot_id / "snapshot.db"
