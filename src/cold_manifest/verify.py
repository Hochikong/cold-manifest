"""盘上副本与源文件完整性校验（verify-copy 核心）。

两层校验，互相独立：

1. **副本对账**（总是做）：盘上副本 `<扫描根>/_coldmanifest/<volume_id>/<ts>/snapshot.db`
   的当前 SHA256 与**采集时记录值**（catalog.on_disk_copies.sha256）对比。
   不能拿主机库现算值当基准——采集后 hash/build-fts/build-stats 都会改主机
   snapshot.db；主机库哈希仅作信息项（host_status）。同时核对旁车
   snapshot.json 的 volume_id / collect_time / files / total_bytes。
   无 on_disk_copies 记录（import-legacy / --no-on-disk-copy）→
   copy.status='not_recorded'，不算失败但副本校验不可用。
2. **源文件抽检**（sample=N 或 full=True）：从扫描根按相对路径回源重算
   SHA256，与条目 hash_hex 比对。只对 hash_state='full' 且快照 meta
   hash_policy='full' 的条目有效——policy 非 full 时明确报"不可用"，
   不静默跳过（sampled 指纹不能证明内容等值）。

只读操作：快照库 immutable 只读、catalog 只查询，不持写锁、不写任何文件。
扫描根解析优先级与 hash.py::_resolve_host_path 同口径：
root_override > meta 的 scan_root/root_path（须为目录）> catalog host_path
（须为目录）> 报错并提示 --root。
"""

import hashlib
import json
import random
import sqlite3
from datetime import datetime
from pathlib import Path

from .catalog import connect_catalog, find_snapshot, snapshot_path
from .db import open_snapshot
from .hash import READ_BLOCK

ProgressCb = "Callable[[int, int], None] | None"

# 副本状态
COPY_OK = "ok"
COPY_MISMATCH = "mismatch"
COPY_MISSING_DISK = "missing_disk"
COPY_NOT_RECORDED = "not_recorded"

# 抽检可用的 hash_state：hash.py 实际写入的枚举是 full/sampled/error（无 'ok'）
HASH_STATE_OK = "full"


class VerifyError(Exception):
    """校验无法进行（快照不存在/未封库/扫描根不可用等）。调用方转 400/CLI 错误。"""


# ---------------------------------------------------------------- 基础件


def _sha256_file(path: Path) -> str:
    """流式计算文件 SHA256（1MB 块，与 hash.py 同块大小）。文件缺失/不可读抛 OSError。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(READ_BLOCK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _meta_get(conn: sqlite3.Connection, key: str) -> "str | None":
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row and row[0] is not None else None


def _resolve_scan_root(snap_conn: sqlite3.Connection, cat_row: sqlite3.Row,
                       root_override: "str | Path | None") -> Path:
    """定位扫描根（优先级见模块 docstring）。与 hash.py::_resolve_host_path 同口径。"""
    if root_override is not None:
        p = Path(root_override)
        if not p.is_dir():
            raise VerifyError(f"指定的扫描根不存在或不是目录：{root_override}")
        return p
    # 快照 meta 的 scan_root / root_path（采集时写入，与 host_path 同值）
    for key in ("scan_root", "root_path"):
        v = _meta_get(snap_conn, key)
        if v and Path(v).is_dir():
            return Path(v)
    host_path = cat_row["host_path"] if cat_row is not None else None
    if host_path and Path(host_path).is_dir():
        return Path(host_path)
    raise VerifyError(
        "无法定位扫描根（meta 的 scan_root/root_path 与 catalog 的 host_path "
        "均不可用，盘未挂载或已换机？可用 --root <盘挂载根> 显式指定）")


def _verify_copy(host_db: Path, disk_db: Path, recorded_sha256: "str | None") -> dict:
    """副本对账：盘上副本当前 sha256 vs 采集时记录值（catalog.on_disk_copies.sha256）。

    不能拿主机库现算值当基准——采集后用户还会跑 hash/build-fts/build-stats，
    都会改主机 snapshot.db，"主机 vs 副本"必然不一致（误报）。主机库哈希只作
    信息项：host_status=modified_since_collection 表示采集后动过主机库。
    """
    host_sha = disk_sha = None
    try:
        if disk_db.is_file():
            disk_sha = _sha256_file(disk_db)
    except OSError as exc:
        raise VerifyError(f"盘上副本不可读：{exc}") from exc
    host_sha = _sha256_file(host_db)  # 主机库存在性已在入口校验

    if not recorded_sha256:
        status = COPY_NOT_RECORDED
    elif not disk_sha:
        status = COPY_MISSING_DISK
    elif disk_sha == recorded_sha256:
        status = COPY_OK
    else:
        status = COPY_MISMATCH
    return {"status": status,
            "recorded_sha256": recorded_sha256,
            "disk_sha256": disk_sha,
            "host_sha256": host_sha,
            "host_status": "unchanged" if host_sha == recorded_sha256
            else "modified_since_collection"}


def _verify_sidecar(copy_dir: Path, volume_id: str, ts: str,
                    file_count: int, total_bytes: int) -> dict:
    """核对旁车 snapshot.json 与快照库统计是否一致。缺失记 status='missing'。"""
    sidecar_path = copy_dir / "snapshot.json"
    if not sidecar_path.is_file():
        return {"status": "missing", "problems": ["snapshot.json 不存在"]}
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"status": "problems", "problems": [f"snapshot.json 不可解析：{exc}"]}
    expected_collect_time = datetime.strptime(
        ts, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
    checks = (
        ("volume_id", data.get("volume_id"), volume_id),
        ("collect_time", data.get("collect_time"), expected_collect_time),
        ("files", data.get("files"), file_count),
        ("total_bytes", data.get("total_bytes"), total_bytes),
    )
    problems = [f"{k} 不一致：旁车={got!r} 快照库={exp!r}"
                for k, got, exp in checks if got != exp]
    return {"status": "ok" if not problems else "problems", "problems": problems}


# ---------------------------------------------------------------- 抽检


def _pick_samples(snap_conn: sqlite3.Connection, sample: int, full: bool,
                  seed: "int | None") -> "list[sqlite3.Row]":
    """从 hash_state='full'（真实整文件哈希）的文件条目中抽样。full=True 全取。"""
    rows = snap_conn.execute(
        "SELECT entry_id, path, size_bytes, hash_hex FROM entries"
        " WHERE type='file' AND hash_state=? AND hash_hex IS NOT NULL"
        " ORDER BY entry_id", (HASH_STATE_OK,)).fetchall()
    if full or sample >= len(rows):
        return list(rows)
    rng = random.Random(seed)
    return rng.sample(rows, sample)


def _verify_source(snap_conn: sqlite3.Connection, scan_root: Path,
                   sample: int, full: bool, seed: "int | None",
                   progress_cb: "ProgressCb") -> dict:
    """源文件抽检：回源重算 SHA256 与条目 hash_hex 比对，分类计数。"""
    result = {"available": True, "checked": 0, "match": 0, "mismatch": 0,
              "missing": 0, "unreadable": 0, "samples": []}
    if not (sample > 0 or full):
        return result
    policy = _meta_get(snap_conn, "hash_policy")
    if policy != "full":
        result["available"] = False
        result["error"] = (
            f"源文件抽检不可用：快照 hash_policy={policy!r}（需 full，"
            "sampled 指纹不能证明内容等值；可先 cldm hash --policy full 重算）")
        return result

    picked = _pick_samples(snap_conn, sample, full, seed)
    total = len(picked)
    # samples 明细上限：异常项全记，match 只记前 N 条，防 full=True 大快照撑爆返回值
    MATCH_SAMPLE_CAP = 100
    match_recorded = 0
    for done, row in enumerate(picked, 1):
        path = row["path"]
        fs_path = scan_root.joinpath(*path.split("/"))
        try:
            actual = _sha256_file(fs_path)
            status = "match" if actual == row["hash_hex"] else "mismatch"
        except FileNotFoundError:
            actual, status = None, "missing"
        except OSError:
            actual, status = None, "unreadable"
        result[status] += 1
        result["checked"] += 1
        if status != "match" or match_recorded < MATCH_SAMPLE_CAP:
            result["samples"].append(
                {"path": path, "expected": row["hash_hex"],
                 "actual": actual, "status": status})
            if status == "match":
                match_recorded += 1
        if progress_cb is not None:
            progress_cb(done, total)
    return result


# ---------------------------------------------------------------- 入口


def verify_snapshot_copy(data_root: "str | Path", snapshot_id: str, *,
                         sample: int = 0, seed: "int | None" = None,
                         full: bool = False, root_override: "str | Path | None" = None,
                         progress_cb: "ProgressCb" = None) -> dict:
    """校验快照的盘上副本与（可选）源文件完整性，返回可直接打印/包装的结果 dict。

    - 副本对账总做；sample>0 或 full=True 时追加源文件抽检；
    - 快照未注册 / 主机快照库缺失 / 未封库 / 扫描根不可定位 → VerifyError；
    - 盘上副本缺失不算异常，记 copy.status='missing_disk'；
    - progress_cb(done, total)：抽检进度（副本对账只有 2 个文件，不回调）。
    """
    if sample < 0:
        raise VerifyError(f"sample 不能为负：{sample}")

    cat = connect_catalog(data_root)
    try:
        cat_row = find_snapshot(cat, snapshot_id)
        if cat_row is None:
            raise VerifyError(f"快照未注册：{snapshot_id}")
        row = cat.execute(
            "SELECT sha256 FROM on_disk_copies WHERE snapshot_id=?",
            (snapshot_id,)).fetchone()
        recorded_sha = row["sha256"] if row is not None else None
    finally:
        cat.close()

    host_db = snapshot_path(data_root, snapshot_id)
    if not host_db.is_file():
        raise VerifyError(f"主机快照库缺失：{host_db}")

    snap_conn = open_snapshot(host_db)
    try:
        if _meta_get(snap_conn, "status") != "sealed":
            raise VerifyError(f"快照未封库，副本尚在变动，不能校验：{snapshot_id}")

        volume_id, ts = snapshot_id.split("/", 1)
        file_count = snap_conn.execute(
            "SELECT COUNT(*) FROM entries WHERE type='file'").fetchone()[0]
        total_bytes = snap_conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM entries"
            " WHERE type='file'").fetchone()[0]

        scan_root = _resolve_scan_root(snap_conn, cat_row, root_override)
        copy_dir = scan_root / "_coldmanifest" / volume_id / ts
        disk_db = copy_dir / "snapshot.db"

        copy = _verify_copy(host_db, disk_db, recorded_sha)
        sidecar = _verify_sidecar(copy_dir, volume_id, ts, file_count, total_bytes)
        source = _verify_source(snap_conn, scan_root, sample, full, seed,
                                progress_cb)
    except sqlite3.DatabaseError as exc:
        raise VerifyError(f"主机快照库无法读取（损坏或非快照库）：{host_db}（{exc}）") from exc
    finally:
        snap_conn.close()

    source_bad = (source["mismatch"] + source["missing"]
                  + source["unreadable"]) if source["available"] else 0
    ok = (copy["status"] in (COPY_OK, COPY_NOT_RECORDED)
          and sidecar["status"] == "ok"
          and source_bad == 0)
    return {
        "snapshot_id": snapshot_id,
        "host_db": str(host_db),
        "disk_db": str(disk_db),
        "copy": copy,
        "sidecar": sidecar,
        "source": source,
        "ok": ok,
    }
