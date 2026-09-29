"""按需文件哈希引擎（§4.5）：给已有快照补算文件哈希（默认关闭、按需启用）。

要点：
- 哈希是**可选阶段**：不参与采集 6 阶段进度，事后对封库快照补算（可续算）；
- policy=full 整文件哈希（sha256，与 sha256sum 逐字节一致）；sampled 只读
  首尾 sample_bytes 并把文件大小混入哈希（大文件快速指纹，非内容等值证明）；
- 跨快照缓存：catalog.hash_cache 按 (size_bytes, mtime_ns, path_norm, algo)
  复用哈希值——冷备多代快照间绝大多数文件不变，命中即免读盘；
- 可续算：只处理 hash_hex IS NULL 的条目，中断后重跑自动跳过已算的；
- 只读打开数据文件（1MB 大块读），路径用 catalog.snapshots.host_path +
  entries.path（相对路径，'/' 分隔）拼接。

写入面：快照库 entries（hash_algo/hash_hex/hash_state）+ meta，catalog 的
hash_cache 表——均为写操作，调用方（CLI / 任务线程）须持 data_root 写锁。
"""

import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .catalog import connect_catalog, find_snapshot, snapshot_path

if TYPE_CHECKING:
    from threading import Event

ProgressCb = Callable[[str, int, int], None]

# 读文件块大小（1MB 大块读，减少 syscall 次数）
READ_BLOCK = 1024 * 1024

# 批量回写事务大小：每 N 个条目提交一次（中断丢的最多是当前批）
BATCH_SIZE = 2000

ALLOWED_ALGOS = ("sha256",)
ALLOWED_POLICIES = ("full", "sampled")


class HashError(Exception):
    """哈希引擎级错误（快照不存在 / host_path 不可用 / 参数非法）。"""


# ---------------------------------------------------------------- 单文件


def _hash_file_strict(path: "str | Path", algo: str = "sha256",
                      policy: str = "full", sample_bytes: int = 65536) -> str:
    """哈希单个文件；任何 OSError 向上抛（由调用方决定记 error 还是失败）。"""
    if policy not in ALLOWED_POLICIES:
        raise HashError(f"非法哈希策略：{policy!r}（允许 {'/'.join(ALLOWED_POLICIES)}）")
    h = hashlib.new(algo)
    if policy == "full":
        with open(path, "rb") as f:
            while True:
                chunk = f.read(READ_BLOCK)
                if not chunk:
                    break
                h.update(chunk)
    else:  # sampled：首尾 sample_bytes + 文件大小混入
        size = os.stat(path).st_size
        h.update(f"cldm-sampled:{algo}:{size}".encode("utf-8"))
        with open(path, "rb") as f:
            if size <= 2 * sample_bytes:
                # 小文件退化为整文件
                while True:
                    chunk = f.read(READ_BLOCK)
                    if not chunk:
                        break
                    h.update(chunk)
            else:
                remaining = sample_bytes
                while remaining > 0:
                    chunk = f.read(min(READ_BLOCK, remaining))
                    if not chunk:
                        break
                    h.update(chunk)
                    remaining -= len(chunk)
                f.seek(size - sample_bytes)
                remaining = sample_bytes
                while remaining > 0:
                    chunk = f.read(min(READ_BLOCK, remaining))
                    if not chunk:
                        break
                    h.update(chunk)
                    remaining -= len(chunk)
    return h.hexdigest()


def hash_file(path: "str | Path", algo: str = "sha256", policy: str = "full",
              sample_bytes: int = 65536) -> "str | None":
    """哈希单个文件；stat/打开失败返回 None（不抛）。"""
    try:
        return _hash_file_strict(path, algo=algo, policy=policy, sample_bytes=sample_bytes)
    except OSError:
        return None


# ---------------------------------------------------------------- 快照级


def _resolve_host_path(snapshot_conn: sqlite3.Connection,
                       catalog_conn: sqlite3.Connection, snapshot_id: str,
                       root: "str | Path | None" = None) -> Path:
    """定位快照源目录。

    优先级：显式 root 覆盖 > catalog.snapshots.host_path（须为目录）>
    host_path 指向 snapshot.db 文件（旧版采集的脏数据）时回退快照库 meta
    的 root_path / scan_root > 报错。
    """
    def _meta_path(key: str) -> "Path | None":
        try:
            r = snapshot_conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            return None
        return Path(r[0]) if r and r[0] else None

    if root is not None:
        p = Path(root)
        if not p.is_dir():
            raise HashError(f"指定的源目录不存在或不是目录：{root}")
        return p

    row = find_snapshot(catalog_conn, snapshot_id)
    if row is None:
        raise HashError(f"快照未注册：{snapshot_id}")
    host_path = Path(row["host_path"]) if row["host_path"] else None
    if host_path is not None and host_path.is_dir():
        return host_path
    # 脏数据回退：旧版采集把 host_path 写成 snapshot.db 文件路径
    for key in ("root_path", "scan_root"):
        mp = _meta_path(key)
        if mp is not None and mp.is_dir():
            return mp
    hint = "（可用 `--root <盘挂载根>` 或 API body 的 root 指定源目录）"
    if host_path is not None and host_path.is_file() and host_path.suffix == ".db":
        raise HashError(f"快照源目录不可用（host_path 指向快照库而非源目录，"
                        f"盘未挂载或已换机？）：{host_path} {hint}")
    raise HashError(f"快照源目录不可用（盘未挂载？）：{row['host_path']} {hint}")


def _entry_fs_path(host_path: Path, rel_path: str) -> Path:
    """entries.path（相对路径，'/' 分隔）→ 本机路径（兼容 Windows 盘符场景）。"""
    return host_path.joinpath(*rel_path.split("/"))


def hash_snapshot(conn: sqlite3.Connection, data_root: "str | Path", snapshot_id: str,
                  *, algo: str = "sha256", policy: str = "full",
                  progress_cb: "ProgressCb | None" = None,
                  cancel_event: "Event | None" = None,
                  catalog_conn: "sqlite3.Connection | None" = None,
                  limit: "int | None" = None,
                  root: "str | Path | None" = None) -> dict:
    """为快照补算文件哈希（可续算）。

    conn：**可写**打开的快照库连接（immutable 只读连接无法回写）。
    遍历 type='file' 且 hash_hex IS NULL 的条目（entry_id 升序）：
    - 缓存命中（catalog.hash_cache 同 size/mtime/path_norm/algo）→ 直接写回，
      hash_state='cached'，不读盘；
    - 未命中 → 读源文件计算，写回 hash_state='full'|'sampled' 并写缓存；
    - 文件不可读 → hash_state='error'，error 记原因，不中断整体。
    每 BATCH_SIZE 条一个事务提交；结束时写 meta（hash_policy/hash_algo/hash_scope）。

    limit：本次最多处理的条目数（调试/测试用，None=不限）。
    返回统计 dict。
    """
    if algo not in ALLOWED_ALGOS:
        raise HashError(f"不支持的哈希算法：{algo!r}（允许 {'/'.join(ALLOWED_ALGOS)}）")
    if policy not in ALLOWED_POLICIES:
        raise HashError(f"非法哈希策略：{policy!r}（允许 {'/'.join(ALLOWED_POLICIES)}）")

    own_catalog = catalog_conn is None
    cat = catalog_conn if own_catalog is False else connect_catalog(data_root)
    try:
        host_path = _resolve_host_path(conn, cat, snapshot_id, root=root)

        total = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE type='file' AND hash_hex IS NULL"
        ).fetchone()[0]
        if limit is not None:
            total = min(total, limit)

        done = computed = cached = errors = 0
        bytes_hashed = 0
        last_id = 0
        t0 = time.monotonic()

        while True:
            if limit is not None and done >= limit:
                break
            batch = BATCH_SIZE if limit is None else min(BATCH_SIZE, limit - done)
            rows = conn.execute(
                "SELECT entry_id, path, path_norm, size_bytes, mtime_ns FROM entries"
                " WHERE type='file' AND hash_hex IS NULL AND entry_id > ?"
                " ORDER BY entry_id LIMIT ?",
                (last_id, batch),
            ).fetchall()
            if not rows:
                break

            updates: "list[tuple[str, str, str, int]]" = []   # (algo, hex, state, entry_id)
            cache_puts: "list[tuple[Any, ...]]" = []
            for r in rows:
                if cancel_event is not None and cancel_event.is_set():
                    _flush(conn, cat, updates, cache_puts)
                    raise RuntimeError("哈希已取消")
                last_id = r["entry_id"]
                done += 1

                cache_hex = _cache_lookup(cat, r, algo)
                if cache_hex is not None:
                    updates.append((algo, cache_hex, "cached", r["entry_id"]))
                    cached += 1
                    continue

                fs_path = _entry_fs_path(host_path, r["path"])
                try:
                    hex_ = _hash_file_strict(fs_path, algo=algo, policy=policy)
                except OSError as e:
                    # hash_hex 记 ''（非 NULL）：续算时视为已处理，不再反复重试
                    # 永久缺失的文件；error 列留原因
                    updates.append((algo, "", "error", r["entry_id"]))
                    conn.execute(
                        "UPDATE entries SET error=? WHERE entry_id=?",
                        (f"hash: {e}", r["entry_id"]),
                    )
                    errors += 1
                    continue

                updates.append((algo, hex_, policy, r["entry_id"]))
                computed += 1
                if r["size_bytes"]:
                    bytes_hashed += r["size_bytes"]
                _cache_put(cat, r, algo, hex_, policy, cache_puts)

            _flush(conn, cat, updates, cache_puts)
            if progress_cb is not None:
                progress_cb("hash", done, total)

        elapsed = time.monotonic() - t0
        # meta 收尾（本次运行正常结束；取消的现场留给下次续算完成时写）
        for k, v in (("hash_policy", policy), ("hash_algo", algo),
                     ("hash_scope", "incremental")):
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)", (k, v))
        conn.commit()

        return {
            "snapshot_id": snapshot_id,
            "algo": algo,
            "policy": policy,
            "total": total,
            "computed": computed,
            "cached": cached,
            "errors": errors,
            "bytes_hashed": bytes_hashed,
            "elapsed_s": round(elapsed, 3),
            "host_path": str(host_path),
        }
    finally:
        if own_catalog:
            cat.close()


# ---------------------------------------------------------------- 内部辅助


def _cache_lookup(cat: sqlite3.Connection, row: sqlite3.Row, algo: str) -> "str | None":
    """查跨快照缓存；size/mtime 缺失（无法构成缓存键）时不查。"""
    if row["size_bytes"] is None or row["mtime_ns"] is None:
        return None
    path_norm = row["path_norm"] if row["path_norm"] is not None else row["path"].casefold()
    r = cat.execute(
        "SELECT hash_hex FROM hash_cache"
        " WHERE size_bytes=? AND mtime_ns=? AND path_norm=? AND algo=?",
        (row["size_bytes"], row["mtime_ns"], path_norm, algo),
    ).fetchone()
    return r[0] if r else None


def _cache_put(cat: sqlite3.Connection, row: sqlite3.Row, algo: str, hex_: str,
               policy: str, out: list) -> None:
    if row["size_bytes"] is None or row["mtime_ns"] is None:
        return
    path_norm = row["path_norm"] if row["path_norm"] is not None else row["path"].casefold()
    out.append((row["size_bytes"], row["mtime_ns"], path_norm, algo, hex_, policy,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))


def _flush(conn: sqlite3.Connection, cat: sqlite3.Connection,
           updates: "list[tuple[str, str, str, int]]", cache_puts: list) -> None:
    """一批回写（entries + error 列已即时写；此处写 hash 三列）并提交两个库。"""
    if updates:
        conn.executemany(
            "UPDATE entries SET hash_algo=?, hash_hex=?, hash_state=? WHERE entry_id=?",
            updates,
        )
    conn.commit()
    if cache_puts:
        cat.executemany(
            "INSERT OR REPLACE INTO hash_cache(size_bytes, mtime_ns, path_norm, algo,"
            " hash_hex, policy, created_at) VALUES(?,?,?,?,?,?,?)",
            cache_puts,
        )
        cat.commit()


# ---------------------------------------------------------------- 任务入口


def run_hash_task(payload: dict, progress_cb: "ProgressCb | None" = None,
                  cancel_event: "Event | None" = None) -> dict:
    """后台任务 / CLI 共用执行体：持 data_root 写锁后同步执行哈希。

    payload: {snapshot_id, algo, policy, data_root}。
    data_root 被其他写者占用 → RuntimeError（任务落 error，与 collect 口径一致）。
    """
    from .lockfile import DataRootLock, LockBusy

    data_root = Path(payload["data_root"])
    lock = DataRootLock(data_root)
    try:
        lock.acquire()
    except LockBusy as e:
        raise RuntimeError(f"数据根被占用：{e}") from None
    try:
        db_path = snapshot_path(data_root, payload["snapshot_id"])
        if not db_path.is_file():
            raise HashError(f"快照库文件缺失：{payload['snapshot_id']}")
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            return hash_snapshot(
                conn, data_root, payload["snapshot_id"],
                algo=payload.get("algo") or "sha256",
                policy=payload.get("policy") or "full",
                progress_cb=progress_cb, cancel_event=cancel_event,
                root=payload.get("root"),
            )
        finally:
            conn.close()
    finally:
        lock.release()
