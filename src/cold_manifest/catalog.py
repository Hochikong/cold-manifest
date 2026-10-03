"""catalog.db 注册表操作：磁盘 / 卷 / 批次 / 快照登记（§5.2）。"""

import os
import re
import sqlite3
import stat
import sys
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


def _usable_serial(serial: "str | None") -> bool:
    """探测序列号是否可用（公共工具：collect 与身份冲突检测共用）。

    视为不可用：空串、全 0 / 全占位符、清洗为 volume_id 字符集后不合法
    （首字符非字母数字）的值——廉价 USB 桥常返回 "0"、"0000000" 甚至带
    控制字节的残串（真盘 F: 实测 '\\x030'），这些值直接拼 volume_id 必然
    被 validate_volume_id 拒绝，应走卷序列号回退或显式 --serial。
    """
    s = (serial or "").strip()
    if not s:
        return False
    # 按 volume_id 的清洗口径检查（控制字符/空格等替换为 _ 后再判）：
    # 首字符须为字母数字（validate_volume_id 同口径）
    sanitized = re.sub(r"[^A-Za-z0-9_.\-]", "_", s)
    if not re.match(r"[A-Za-z0-9]", sanitized):
        return False
    # 已知占位形态（USB 桥常见，非真序列号）：
    # ① 纯 0 串（000/0000000…）；② 0123456789ABCDEF（JMicron 等桥固件
    #    的出厂占位，大小写不敏感——两块同款盒会报同一个值，直接撞车）；
    # ③ 全同字符串（xxxxxxxx、AAAAAAAA…）。
    if sanitized.upper() == "0123456789ABCDEF":
        return False
    if len(set(sanitized.upper())) == 1:
        return False
    return True


_IDENTITY_HISTORY_MAX = 20


def _norm_model(model: "str | None") -> str:
    """型号归一化：去空白、大写（用于同型号不同写法的比较）。"""
    return re.sub(r"\s+", "", model or "").upper()


def _detect_identity_conflicts(existing: Any, incoming: dict) -> "list[dict]":
    """比对既有磁盘行与本次探测字段，逐条给出身份冲突（field/existing/incoming）。

    判定口径：
    ① 两侧 capacity_bytes 均非空且差 >20%；
    ② 型号归一化（去空白、大写）后不同（physical_model 优先、回退 bridge_model）；
    ③ 两侧"可用序列号"均非空且不同（physical_serial 优先、回退 disk_id——
       disk_id 即规范化 serial）。
    """
    conflicts: "list[dict]" = []
    ec = existing["capacity_bytes"]
    ic = incoming.get("capacity_bytes")
    if ec and ic and max(ec, ic) * 100 > min(ec, ic) * 120:
        conflicts.append({"field": "capacity_bytes",
                          "existing": ec, "incoming": ic})
    em = _norm_model(existing["physical_model"] or existing["bridge_model"])
    im = _norm_model(incoming.get("physical_model")
                     or incoming.get("bridge_model"))
    if em and im and em != im:
        conflicts.append({"field": "physical_model",
                          "existing": existing["physical_model"] or existing["bridge_model"],
                          "incoming": incoming.get("physical_model")
                          or incoming.get("bridge_model")})
    es = existing["physical_serial"]
    if not _usable_serial(es):
        # 既有行物理序列号缺失/占位（旧版仅按 probe 登记）→ 本次回填真值，
        # 不构成冲突（probe 撞车的两块盘不会走到同一行：find_disk_by_identity
        # 已按物理序列号把它们分开）
        es = None
    iser = incoming.get("physical_serial")
    if not _usable_serial(iser):
        iser = None
    if es and iser and (es or "").strip() != (iser or "").strip():
        conflicts.append({"field": "physical_serial",
                          "existing": es, "incoming": iser})
    return conflicts


def ensure_disk(conn: sqlite3.Connection, disk_id: str, **fields: Any) -> dict:
    """登记磁盘（幂等）：已存在则只刷新 last_seen，其余字段不动。

    返回 {"inserted": bool, "conflicts": [...]}。conflicts 非空表示既有
    记录与本次探测身份不一致（两块盘可能报出相同 disk_id）：此时**不覆盖
    任何既有字段**，只刷 last_seen 并把冲突追加进 identity_conflict_json
    （最多保留最近 20 条）、置 identity_verified=0。
    identity_verified 由 caller 显式传入（若非 None）时，在无冲突路径上
    写入/更新（含首次插入）。
    """
    from datetime import datetime, timezone
    import json

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    identity_verified = fields.pop("identity_verified", None)
    cur = conn.execute("SELECT * FROM disks WHERE disk_id=?", (disk_id,))
    row = cur.fetchone()
    # 兼容无 row_factory 的连接（如 tests/test_hash.py 裸 sqlite3.connect）：
    # 统一按列名取值（cursor.description 拼 dict）
    existing = dict(zip((d[0] for d in cur.description), row)) if row is not None else None
    conflicts = _detect_identity_conflicts(existing, fields) if existing else []
    if conflicts:
        # 冲突：不改既有字段，只追加冲突记录 + 刷 last_seen
        old = existing.get("identity_conflict_json")
        try:
            history = json.loads(old) if old else []
        except (TypeError, ValueError):
            history = []
        history.append({"at": now, "conflicts": conflicts})
        history = history[-_IDENTITY_HISTORY_MAX:]
        conn.execute(
            "UPDATE disks SET last_seen=?, identity_verified=0,"
            " identity_conflict_json=? WHERE disk_id=?",
            (now, json.dumps(history, ensure_ascii=False), disk_id))
        return {"inserted": False, "conflicts": conflicts}

    cols = {"disk_id": disk_id, "first_seen": now, "last_seen": now, **fields}
    if identity_verified is not None:
        cols["identity_verified"] = identity_verified
    keys = list(cols)
    skip_updates = ("disk_id", "first_seen", "last_seen", "nickname",
                    "identity_conflict_json")
    updates = ("last_seen=excluded.last_seen, " + ", ".join(
        f"{k}=COALESCE(excluded.{k}, {k})"
        for k in keys if k not in skip_updates)).rstrip(", ")
    if not updates:
        # 只有 disk_id/first_seen/last_seen（无附加字段）：UPSERT 只刷 last_seen
        updates = "last_seen=excluded.last_seen"
    conn.execute(
        f"INSERT INTO disks({','.join(keys)}) VALUES({','.join('?' * len(keys))}) "
        f"ON CONFLICT(disk_id) DO UPDATE SET {updates}",
        [cols[k] for k in keys],
    )
    return {"inserted": existing is None, "conflicts": []}


def _sanitize_serial(serial: "str | None") -> str:
    """序列号清洗为合法 volume_id 字符集（控制字符/空格/斜杠等 → '_'）。"""
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", (serial or "").strip())


def find_disk_by_identity(conn: sqlite3.Connection, *, probe_serial: "str | None",
                          physical_serial: "str | None") -> "str | None":
    """按身份查既有磁盘行，返回可复用的 disk_id（找不到返回 None）。

    查找顺序：
    ① 物理序列号可用时，按其比对 disks.disk_id（登记时的规范化 serial）
       或 disks.physical_serial —— 命中即同一块盘，复用（历史不分裂）；
    ② 再按探测序列号比对同两列 —— 命中时须防"probe 撞车"：候选行的
       physical_serial 与本次物理序列号均可用且不同 → 是两块不同的盘
       （USB 盒常见：桥固件报出相同序列号），**不**复用，由 caller 按
       物理序列号另立新盘；候选行物理序列号缺失/一致 → 视为同一块盘复用。
    """
    phys = (physical_serial or "").strip()
    if _usable_serial(phys):
        phys_norm = _sanitize_serial(phys)
        rows = conn.execute(
            "SELECT disk_id FROM disks WHERE disk_id=? COLLATE NOCASE"
            " OR disk_id=? COLLATE NOCASE OR physical_serial=? COLLATE NOCASE",
            (phys, phys_norm, phys)).fetchall()
        if rows:
            return rows[0][0]
    probe = (probe_serial or "").strip()
    if not _usable_serial(probe):
        return None
    probe_norm = _sanitize_serial(probe)
    rows = conn.execute(
        "SELECT disk_id, physical_serial FROM disks WHERE disk_id=? COLLATE NOCASE"
        " OR disk_id=? COLLATE NOCASE OR physical_serial=? COLLATE NOCASE",
        (probe, probe_norm, probe)).fetchall()
    for row in rows:
        existing_phys = (row[1] or "").strip()
        if (_usable_serial(existing_phys) and _usable_serial(phys)
                and existing_phys.upper() != phys.upper()):
            continue  # probe 撞车且物理序列号不同 → 不同盘，跳过
        return row[0]
    return None


def find_probe_collision(conn: sqlite3.Connection, *, probe_serial: "str | None",
                         physical_serial: "str | None") -> "str | None":
    """probe 撞车检测：探测序列号命中、但既有行物理序列号与本次不同的盘。

    返回撞车的既有 disk_id（用于把冲突记录照旧写到那块盘上），无撞车返回 None。
    """
    probe = (probe_serial or "").strip()
    if not _usable_serial(probe):
        return None
    phys = (physical_serial or "").strip()
    rows = conn.execute(
        "SELECT disk_id, physical_serial FROM disks WHERE disk_id=? COLLATE NOCASE"
        " OR disk_id=? COLLATE NOCASE OR physical_serial=? COLLATE NOCASE",
        (probe, _sanitize_serial(probe), probe)).fetchall()
    for row in rows:
        existing_phys = (row[1] or "").strip()
        if (_usable_serial(existing_phys) and _usable_serial(phys)
                and existing_phys.upper() != phys.upper()):
            return row[0]
    return None


def find_volume_by_partition(conn: sqlite3.Connection, *, disk_id: str,
                             partition_index: "int | None") -> "str | None":
    """查某盘下同分区号的既有卷，返回可复用的 volume_id（没有则 None）。"""
    row = conn.execute(
        "SELECT volume_id FROM volumes WHERE disk_id=? AND partition_index IS ?"
        " ORDER BY volume_id LIMIT 1", (disk_id, partition_index)).fetchone()
    return row[0] if row else None


def preferred_disk_serial(physical_serial: "str | None",
                          probe_serial: "str | None") -> str:
    """新盘命名序列号：物理序列号可用优先，其次探测序列号；都不可用返回 ''。"""
    for cand in ((physical_serial or "").strip(), (probe_serial or "").strip()):
        if _usable_serial(cand):
            return _sanitize_serial(cand)
    return ""


def ensure_volume(conn: sqlite3.Connection, volume_id: str, disk_id: str, **fields: Any) -> None:
    """登记卷（幂等）：已存在则保留已有真值、回填缺失字段（如 partition_uuid）。"""
    cols = {"volume_id": volume_id, "disk_id": disk_id, **fields}
    keys = list(cols)
    updates = ", ".join(f"{k}=COALESCE(excluded.{k}, {k})" for k in keys if k not in ("volume_id", "nickname"))
    conn.execute(
        f"INSERT INTO volumes({','.join(keys)}) VALUES({','.join('?' * len(keys))}) "
        f"ON CONFLICT(volume_id) DO UPDATE SET {updates}",
        [cols[k] for k in keys],
    )


def snapshot_label(conn: sqlite3.Connection, snapshot_id: str) -> str:
    """快照展示标签（P1 昵称）：卷昵称（磁盘昵称）逐级回落。

    卷有昵称 → "卷昵（盘昵）"；仅盘有 → "盘昵"；都没有 → ""。
    有昵称时尾部附 " · <volume_id>"；快照/卷行缺失也返回 ""。
    """
    row = conn.execute(
        "SELECT v.volume_id AS volume_id, v.nickname AS vnick, d.nickname AS dnick"
        " FROM snapshots s JOIN volumes v ON v.volume_id = s.volume_id"
        " LEFT JOIN disks d ON d.disk_id = v.disk_id"
        " WHERE s.snapshot_id=?", (snapshot_id,)).fetchone()
    if row is None:
        return ""
    label = ""
    if row[1]:
        label = row[1] + (f"（{row[2]}）" if row[2] else "")
    elif row[2]:
        label = row[2]
    return f"{label} · {row[0]}" if label else ""


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


def rmtree_ro(path: "str | Path") -> None:
    """shutil.rmtree 的 Windows 兼容版：只读文件先 chmod 去只读再删。

    目录不存在时静默返回（与调用点原有的"不存在即跳过"语义一致）。
    文件被其他进程占用（Windows 独占打开）时 PermissionError 照常抛出，
    由调用方决定重试/报错——本函数不吞占用类错误。
    """
    import shutil

    def _onexc(func, p, exc_info):  # type: (object, str, object) -> None
        try:
            os.chmod(p, stat.S_IWRITE)
        except OSError:
            pass
        func(p)

    kwargs: "dict[str, Any]" = {}
    if sys.version_info >= (3, 12):
        kwargs["onexc"] = _onexc
    else:
        kwargs["onerror"] = _onexc
    try:
        shutil.rmtree(path, **kwargs)
    except FileNotFoundError:
        return


class SnapshotDeleteBlocked(Exception):
    """删除被阻塞：被 diff 引用或存在活跃任务（路由层转 409）。

    diffs/tasks：阻塞来源 ID 列表（二选一非空）。
    """

    def __init__(self, message: str, *, diffs: "list[str] | None" = None,
                 tasks: "list[str] | None" = None) -> None:
        super().__init__(message)
        self.diffs = diffs or []
        self.tasks = tasks or []


class ObjectDeleteBlocked(Exception):
    """删除磁盘/卷被阻塞：其下仍存在快照（路由层转 409，不允许级联删快照）。

    snapshots：阻塞来源的 snapshot_id 列表；
    diffs：引用这些快照的 diff_id 列表（供指引，不阻塞判断本身）。
    """

    def __init__(self, message: str, *, snapshots: "list[str] | None" = None,
                 diffs: "list[str] | None" = None) -> None:
        super().__init__(message)
        self.snapshots = snapshots or []
        self.diffs = diffs or []


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
        rmtree_ro(host_dir)
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
                rmtree_ro(copy_dir)
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


def _blocked_snapshots_and_diffs(conn: sqlite3.Connection,
                                 volume_ids: "list[str]") -> "tuple[list[str], list[str]]":
    """列出这些卷下的快照与引用它们的 diff（用于阻塞提示）。"""
    if not volume_ids:
        return [], []
    marks = ",".join("?" * len(volume_ids))
    snap_rows = conn.execute(
        f"SELECT snapshot_id FROM snapshots WHERE volume_id IN ({marks})"
        " ORDER BY snapshot_id", volume_ids).fetchall()
    snap_ids = [r["snapshot_id"] for r in snap_rows]
    diff_ids: "list[str]" = []
    if snap_ids:
        smarks = ",".join("?" * len(snap_ids))
        diff_ids = [r["diff_id"] for r in conn.execute(
            f"SELECT DISTINCT diff_id FROM diff_runs WHERE a IN ({smarks})"
            f" OR b IN ({smarks}) ORDER BY diff_id", snap_ids + snap_ids)]
    return snap_ids, diff_ids


def _defensive_cleanup(conn: sqlite3.Connection, volume_ids: "list[str]",
                       disk_id: "str | None" = None) -> None:
    """防御式清理：on_disk_copies / disk_smart 中指向已不存在快照的残留行。

    正常流程走到删除时快照已清空，这些表应已无对应行；此处兜底清孤儿，
    不删任何仍被 snapshots 引用的行。
    """
    def _like(v: str) -> str:
        # 转义 LIKE 通配符后接未转义的 '%' 作前缀通配
        return (v.replace("\\", "\\\\").replace("%", "\\%")
                 .replace("_", "\\_")) + "%"

    if volume_ids:
        likes = " OR ".join("snapshot_id LIKE ? ESCAPE '\\'" for _ in volume_ids)
        params = [_like(v) for v in volume_ids]
        # 卷前缀匹配 + 不再被 snapshots 引用 = 孤儿残留行
        conn.execute(
            f"DELETE FROM on_disk_copies WHERE ({likes}) AND NOT EXISTS ("
            f"SELECT 1 FROM snapshots s WHERE s.snapshot_id=on_disk_copies.snapshot_id)",
            params)
        if disk_id is None:
            conn.execute(
                f"DELETE FROM disk_smart WHERE ({likes}) AND NOT EXISTS ("
                f"SELECT 1 FROM snapshots s WHERE s.snapshot_id=disk_smart.snapshot_id)",
                params)
    if disk_id is not None:
        conn.execute("DELETE FROM disk_smart WHERE disk_id=?", (disk_id,))


def delete_volume(conn: sqlite3.Connection, data_root: "str | Path",
                  volume_id: str) -> dict:
    """删除卷（catalog 注册行）：其下存在快照 → 抛 ObjectDeleteBlocked。

    - 不允许级联删快照（用户规则：先删快照与比对，再删卷）；
    - volume_id 非法 → SnapshotDeleteError（路由转 400）；
    - 卷不存在 → LookupError（路由转 404）；
    - hash_cache 不动（缓存键与卷无关）。

    返回 {volume_id, deleted_snapshots: 0}。
    """
    del data_root  # 预留：未来卷级残留目录清理；当前 catalog 行删除不需要
    from .import_legacy import LegacyImportError
    try:
        validate_volume_id(volume_id)
    except LegacyImportError as e:
        raise SnapshotDeleteError(str(e)) from None

    if conn.execute("SELECT 1 FROM volumes WHERE volume_id=?",
                    (volume_id,)).fetchone() is None:
        raise LookupError(f"卷不存在：{volume_id}")

    snap_ids, diff_ids = _blocked_snapshots_and_diffs(conn, [volume_id])
    if snap_ids:
        msg = (f"卷 {volume_id} 下仍有 {len(snap_ids)} 个快照："
               f"{', '.join(snap_ids)}；请先删除这些快照与相关对比，再删除卷")
        raise ObjectDeleteBlocked(msg, snapshots=snap_ids, diffs=diff_ids)

    _defensive_cleanup(conn, [volume_id])
    conn.execute("DELETE FROM volumes WHERE volume_id=?", (volume_id,))
    conn.commit()
    return {"volume_id": volume_id, "deleted_snapshots": 0}


def delete_disk(conn: sqlite3.Connection, data_root: "str | Path",
                disk_id: str) -> dict:
    """删除磁盘及其全部卷行：任一卷下存在快照 → 抛 ObjectDeleteBlocked。

    - 不允许级联删快照（用户规则：先删快照与比对，再删盘）；
    - 盘不存在 → LookupError（路由转 404）；
    - hash_cache 不动（缓存键与盘/卷无关）。

    返回 {disk_id, deleted_volumes, deleted_snapshots: 0}。
    """
    del data_root  # 预留：未来盘级残留目录清理；当前 catalog 行删除不需要
    if conn.execute("SELECT 1 FROM disks WHERE disk_id=?",
                    (disk_id,)).fetchone() is None:
        raise LookupError(f"盘不存在：{disk_id}")

    vol_ids = [r["volume_id"] for r in conn.execute(
        "SELECT volume_id FROM volumes WHERE disk_id=? ORDER BY volume_id",
        (disk_id,))]
    snap_ids, diff_ids = _blocked_snapshots_and_diffs(conn, vol_ids)
    if snap_ids:
        msg = (f"盘 {disk_id}（含其卷）下仍有 {len(snap_ids)} 个快照："
               f"{', '.join(snap_ids)}；请先删除这些快照与相关对比，再删除磁盘")
        raise ObjectDeleteBlocked(msg, snapshots=snap_ids, diffs=diff_ids)

    _defensive_cleanup(conn, vol_ids, disk_id=disk_id)
    conn.execute("DELETE FROM volumes WHERE disk_id=?", (disk_id,))
    conn.execute("DELETE FROM disks WHERE disk_id=?", (disk_id,))
    conn.commit()
    return {"disk_id": disk_id, "deleted_volumes": len(vol_ids),
            "deleted_snapshots": 0}


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
