"""v1 旧版快照导入：backuptools 三件套（metadata/tree/warnings.csv）→ snapshot.db + catalog 注册。

数据流（§8 导入兼容）：
  metadata.csv → meta 表 + disk/volume/batch/snapshot 登记
  tree.csv     → entries 表（文件行；目录按需从相对路径推导，dirs_source=derived）
  warnings.csv → skipped 表（按主键去重）
封库：建索引 → 构建 dir_rollup（后代统计）→ PRAGMA optimize。
"""

import calendar
import csv
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .db import file_uri
from .paths import normalize_path
from .catalog import (connect_catalog, create_batch, ensure_disk, ensure_volume, register_snapshot,
                      rmtree_ro, snapshot_path, validate_volume_id)
from .schema import SNAPSHOT_TABLES_DDL

_BATCH_SIZE = 10_000
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:/")


class LegacyImportError(Exception):
    """旧版快照目录缺失文件或解析失败。"""


@dataclass
class ImportResult:
    snapshot_id: str
    volume_id: str
    ts: str
    db_path: Path
    skipped_import: bool = False  # 目标已存在 sealed 快照而被跳过
    db_size: int = 0
    elapsed_s: float = 0.0
    stats: dict = field(default_factory=dict)


def _read_metadata(snapshot_dir: Path) -> dict:
    path = snapshot_dir / "metadata.csv"
    if not path.is_file():
        raise LegacyImportError(f"缺少 metadata.csv：{path}")
    meta: dict = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2:
                if row[0] == "metric_name" and row[1] == "value":
                    continue  # 表头行
                meta[row[0]] = row[1]
    if not meta:
        raise LegacyImportError(f"metadata.csv 为空：{path}")
    return meta


def _parse_ts(collect_time: str) -> str:
    """'2026-05-04T12:09:03Z' → '20260504T120903Z'（目录名用）。"""
    try:
        dt = datetime.strptime(collect_time.strip(), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as e:
        raise LegacyImportError(f"collect_time_utc 解析失败：{collect_time!r}（{e}）") from e
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _iso_to_ns(value: str) -> "int | None":
    """ISO UTC 时间 → ns 整数（calendar.timegm 整数运算，不走浮点）。"""
    if not value:
        return None
    v = value.strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(?:Z|[+-]\d{2}:?\d{2})?$", v)
    if not m:
        return None
    try:
        st = time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    secs = calendar.timegm(st)
    frac = (m.group(2) or "").ljust(9, "0")[:9]
    return secs * 1_000_000_000 + int(frac or "0")


def _ext_of(name: str) -> str:
    """最后一个 '.' 后的后缀（小写、含点）；无点（或点在首位）→ ''。"""
    idx = name.rfind(".")
    if idx <= 0:
        return ""
    return name[idx:].lower()


def _norm_skipped_path(path: str) -> str:
    """warnings 路径规整：反斜杠→斜杠，去掉 'X:/' 盘符前缀。"""
    p = path.replace("\\", "/")
    return _DRIVE_PREFIX.sub("", p)


def _ensure_dir_chain(dir_ids: "dict[str, int]", conn: sqlite3.Connection,
                      rel_dir: str, next_id: "list[int]") -> int:
    """确保 rel_dir 及其所有父目录存在于 entries，返回其 entry_id。

    dir_ids: 已知目录 path → entry_id 缓存；next_id: 单元素列表持有着下一个可用 id。
    """
    if rel_dir == "":
        return 1  # root
    known = dir_ids.get(rel_dir)
    if known is not None:
        return known
    slash = rel_dir.rfind("/")
    parent_path = rel_dir[:slash] if slash >= 0 else ""
    parent_id = _ensure_dir_chain(dir_ids, conn, parent_path, next_id)
    eid = next_id[0]
    next_id[0] += 1
    name = rel_dir[slash + 1:]
    depth = rel_dir.count("/") + 1
    conn.execute(
        "INSERT OR REPLACE INTO entries(entry_id,parent_id,path,name,depth,type,size_bytes,allocated_bytes,"
        "mtime_ns,ctime_ns,btime_ns,attrs,ext,hash_algo,hash_hex,hash_state,path_norm,error)"
        " VALUES(?,?,?,?,?,'dir',NULL,NULL,NULL,NULL,NULL,NULL,'',NULL,NULL,NULL,?,NULL)",
        (eid, parent_id, rel_dir, name, depth, normalize_path(rel_dir)),
    )
    dir_ids[rel_dir] = eid
    return eid


def _build_entries(conn: sqlite3.Connection, tree_path: Path,
                   progress_cb: "Callable[[str, int, int], None] | None" = None,
                   total_rows: "int | None" = None) -> "dict[str, Any]":
    """灌 entries 表，返回统计。progress_cb 在 parse 阶段每约 5 万行回调一次。"""
    if not tree_path.is_file():
        raise LegacyImportError(f"缺少 tree.csv：{tree_path}")
    file_count = dir_count = 0
    total_bytes = 0
    zero_byte = 0
    max_depth = 0
    next_id = [2]  # 1 已被 root 占用
    dir_ids: "dict[str, int]" = {}
    buf: "list[tuple]" = []

    # 根行：entry_id=1, parent_id=0
    conn.execute(
        "INSERT OR REPLACE INTO entries(entry_id,parent_id,path,name,depth,type,size_bytes,allocated_bytes,"
        "mtime_ns,ctime_ns,btime_ns,attrs,ext,hash_algo,hash_hex,hash_state,path_norm,error)"
        " VALUES(1,0,'.','',0,'dir',NULL,NULL,NULL,NULL,NULL,NULL,'',NULL,NULL,NULL,'.',NULL)"
    )

    with tree_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        try:
            i_path = header.index("relative_path")
            i_name = header.index("entry_name")
            i_type = header.index("entry_type")
            i_size = header.index("size_bytes")
            i_mtime = header.index("modified_utc")
        except ValueError as e:
            raise LegacyImportError(f"tree.csv 表头缺少必需列：{e}") from e

        def flush() -> None:
            if buf:
                conn.executemany(
                    "INSERT OR REPLACE INTO entries(entry_id,parent_id,path,name,depth,type,size_bytes,allocated_bytes,"
                    "mtime_ns,ctime_ns,btime_ns,attrs,ext,hash_algo,hash_hex,hash_state,path_norm,error)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    buf,
                )
                buf.clear()


        _PROGRESS_EVERY = 50_000
        n_rows = 0

        for row in reader:
            n_rows += 1
            if progress_cb is not None and n_rows % _PROGRESS_EVERY == 0:
                progress_cb("parse", n_rows, total_rows if total_rows is not None else -1)
            if len(row) <= i_mtime:
                continue
            rel = row[i_path].strip()
            etype = row[i_type].strip()
            if rel == "" or rel == ".":
                continue
            if rel.endswith("/."):  # legacy 目录行偶发 'dir/.' 形式
                rel = rel[:-2]
                if rel == "" or rel in dir_ids:
                    continue
            mtime_ns = _iso_to_ns(row[i_mtime])
            if etype in ("dir", "empty_dir"):
                # 目录行（v1 scanner 对空目录写 token 'empty_dir'）：size 置 NULL；
                # 若路径已由父链推导创建过，回填真实 mtime（推导时为 NULL），不直接跳过
                if rel in dir_ids:
                    if mtime_ns is not None:
                        conn.execute("UPDATE entries SET mtime_ns=? WHERE entry_id=?",
                                     (mtime_ns, dir_ids[rel]))
                    continue
                parent_path = rel.rsplit("/", 1)[0] if "/" in rel else ""
                parent_id = _ensure_dir_chain(dir_ids, conn, parent_path, next_id)
                eid = next_id[0]
                next_id[0] += 1
                name = row[i_name] or rel.rsplit("/", 1)[-1]
                depth = rel.count("/") + 1
                buf.append((eid, parent_id, rel, name, depth, "dir", None, None,
                            mtime_ns, None, None, None, "", None, None, None, normalize_path(rel), None))
                dir_count += 1
                dir_ids[rel] = eid
                max_depth = max(max_depth, depth)
            else:
                slash = rel.rfind("/")
                parent_path = rel[:slash] if slash >= 0 else ""
                parent_id = _ensure_dir_chain(dir_ids, conn, parent_path, next_id)
                eid = next_id[0]
                next_id[0] += 1
                name = row[i_name] or rel[slash + 1:]
                depth = rel.count("/") + 1
                try:
                    size = int(row[i_size]) if row[i_size] else 0
                except ValueError:
                    size = 0
                buf.append((eid, parent_id, rel, name, depth, "file", size, size,
                            mtime_ns, None, None, None, _ext_of(name), None, None, None, normalize_path(rel), None))
                file_count += 1
                total_bytes += size
                if size == 0:
                    zero_byte += 1
                max_depth = max(max_depth, depth)
            if len(buf) >= _BATCH_SIZE:
                flush()
                conn.commit()
        flush()
        conn.commit()
    if progress_cb is not None:
        progress_cb("parse", n_rows, n_rows if total_rows is None else total_rows)
    return {"file_count": file_count, "dir_count_derived": dir_count,
            "total_bytes": total_bytes, "zero_byte_count": zero_byte, "max_depth": max_depth}


def _import_warnings(conn: sqlite3.Connection, snapshot_dir: Path) -> int:
    """warnings.csv → skipped（INSERT OR IGNORE 去重），返回去重后行数。"""
    path = snapshot_dir / "warnings.csv"
    if not path.is_file():
        return 0
    before = conn.execute("SELECT COUNT(*) FROM skipped").fetchone()[0]
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return 0
        try:
            i_stage = header.index("stage")
            i_path = header.index("path")
            i_type = header.index("warning_type")
            i_detail = header.index("detail")
        except ValueError:
            return 0
        buf: "list[tuple]" = []
        for row in reader:
            if len(row) <= i_detail:
                continue
            buf.append((_norm_skipped_path(row[i_path]), row[i_type], row[i_stage], row[i_detail] or None))
            if len(buf) >= _BATCH_SIZE:
                conn.executemany("INSERT OR IGNORE INTO skipped(path,warning_type,stage,detail) VALUES(?,?,?,?)", buf)
                buf.clear()
        if buf:
            conn.executemany("INSERT OR IGNORE INTO skipped(path,warning_type,stage,detail) VALUES(?,?,?,?)", buf)
    return conn.execute("SELECT COUNT(*) FROM skipped").fetchone()[0] - before


def _build_dir_rollup(conn: sqlite3.Connection) -> None:
    """兼容别名：封库 rollup 已抽取到 seal.py（与 collect 共用）。"""
    from .seal import build_dir_rollup

    build_dir_rollup(conn)


def _is_sealed(db_path: Path) -> bool:
    """快照库是否已封库（meta 中存在 status=sealed）。"""
    try:
        conn = sqlite3.connect(file_uri(db_path, immutable=False), uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='status'").fetchone()
        return bool(row) and row[0] == "sealed"
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def import_snapshot(snapshot_dir: "str | Path", data_root: "str | Path",
                    volume_id: "str | None" = None, force: bool = False,
                    progress_cb: "Callable[[str, int, int], None] | None" = None) -> ImportResult:
    """导入一个 v1 快照目录。见模块 docstring。

    progress_cb(phase, done, total)：phase ∈ {"read","parse","seal","done"}；
    parse 的 total 未知时为 -1；可选回调，不传时行为与旧版完全一致。
    """
    t0 = time.monotonic()
    if progress_cb is not None:
        progress_cb("read", 0, 3)
    snapshot_dir = Path(snapshot_dir)
    meta = _read_metadata(snapshot_dir)
    if progress_cb is not None:
        progress_cb("read", 1, 3)
    volume_id = volume_id or meta.get("volume_id") or ""
    if not volume_id:
        raise LegacyImportError("volume_id 未提供且 metadata.csv 中无 volume_id")
    validate_volume_id(volume_id)
    ts = _parse_ts(meta.get("collect_time_utc", ""))
    snapshot_id = f"{volume_id}/{ts}"
    dest_dir = snapshot_path(data_root, snapshot_id).parent
    db_path = dest_dir / "snapshot.db"

    if db_path.exists():
        if _is_sealed(db_path) and not force:
            return ImportResult(snapshot_id=snapshot_id, volume_id=volume_id, ts=ts,
                                db_path=db_path, skipped_import=True, elapsed_s=time.monotonic() - t0)
        # 未封库 = 上次导入崩溃的脏残留，一律删除重建（与 force 无关）
        rmtree_ro(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript(SNAPSHOT_TABLES_DDL)
        conn.execute("BEGIN")

        ent_stats = _build_entries(conn, snapshot_dir / "tree.csv", progress_cb=progress_cb)
        skipped_count = _import_warnings(conn, snapshot_dir)

        if progress_cb is not None:
            progress_cb("seal", 0, 3)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta_rows: "list[tuple[str, str]]" = [(str(k), str(v)) for k, v in meta.items()]
        meta_rows += [
            ("imported_from", "legacy_v1_csv"),
            ("source_dir", str(snapshot_dir.resolve())),
            ("dirs_source", "derived"),
            ("allocated_source", "logical_size_fallback"),
            # 采集平台标记（与 collect._write_meta 同口径）：legacy v1 无此键，
            # 导入发生在哪台机器就记当前平台（仅提示用，不参与比对语义）
            ("os_platform", sys.platform),
            ("import_time_utc", now),
        ]
        conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", meta_rows)
        conn.commit()

        # 封库：索引 → dir_rollup → optimize（seal.py，与 collect 共用）
        from .seal import seal_snapshot

        seal_snapshot(conn)
        # status=sealed 在封库成功后写入（镜像 collect.py）：seal 中途崩溃不应留下
        # 假 sealed 库——否则重导入会被 skip、启动孤儿清扫也不会清理它
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('status', 'sealed')")
        conn.commit()

        if progress_cb is not None:
            progress_cb("seal", 3, 3)
            progress_cb("done", 1, 1)

        stats: "dict[str, Any]" = {
            "file_count": ent_stats["file_count"],
            "dir_count": conn.execute(
                "SELECT COUNT(*) FROM entries WHERE type='dir' AND entry_id != 1").fetchone()[0],
            "total_bytes": conn.execute(
                "SELECT COALESCE(SUM(size_bytes),0) FROM entries WHERE type='file'").fetchone()[0],
            "total_alloc": conn.execute(
                "SELECT COALESCE(SUM(allocated_bytes),0) FROM entries WHERE type='file'").fetchone()[0],
            "zero_byte_count": conn.execute(
                "SELECT COUNT(*) FROM entries WHERE type='file' AND size_bytes=0").fetchone()[0],
            "max_depth": ent_stats["max_depth"],
            "skipped_count": conn.execute("SELECT COUNT(*) FROM skipped").fetchone()[0],
        }
    finally:
        conn.close()

    # catalog 注册
    disk_serial = (meta.get("disk_serial") or "").strip()
    disk_id = disk_serial or f"NOSERIAL_{volume_id}"
    cat = connect_catalog(data_root)
    try:
        ensure_disk(cat, disk_id,
                    physical_model=meta.get("model") or None,
                    capacity_bytes=int(meta["disk_capacity_bytes"]) if meta.get("disk_capacity_bytes") else None,
                    interface_type=meta.get("interface_type") or None)
        ensure_volume(cat, volume_id, disk_id,
                      partition_index=int(meta["partition_index"]) if meta.get("partition_index") else None,
                      filesystem=meta.get("filesystem") or None,
                      label=meta.get("volume_label") or None,
                      capacity_bytes=(int(meta["partition_capacity_bytes"])
                                      if meta.get("partition_capacity_bytes") else None))
        batch_id = create_batch(cat, disk_id, [volume_id])
        register_snapshot(cat, snapshot_id, volume_id,
                          batch_id=batch_id,
                          collected_at=meta.get("collect_time_utc"),
                          collector_version=meta.get("collector_version"),
                          host_path=str(snapshot_dir.resolve()),
                          status="sealed", hash_policy="none",
                          file_count=stats["file_count"], dir_count=stats["dir_count"],
                          total_bytes=stats["total_bytes"], total_alloc=stats["total_alloc"],
                          zero_byte_count=stats["zero_byte_count"], max_depth=stats["max_depth"],
                          skipped_count=stats["skipped_count"])
        cat.commit()
    finally:
        cat.close()

    return ImportResult(snapshot_id=snapshot_id, volume_id=volume_id, ts=ts, db_path=db_path,
                        db_size=db_path.stat().st_size, elapsed_s=time.monotonic() - t0, stats=stats)
