"""SQLite 连接辅助：快照库只读打开，catalog 可写（WAL）。"""

import logging
import sqlite3
from pathlib import Path

from .schema import CATALOG_DDL, SNAPSHOT_DDL


def file_uri(path: "str | Path", *, immutable: bool = True, mode: str = "ro") -> str:
    """构造打开 SQLite 库的 file: URI（全项目唯一构造点）。

    为什么必须统一走这里：Windows 上数据根常形如 `D:\\pkg\\bin\\..\\..\\data`
    （start.cmd 的 %~dp0 展开），直接把未归一化路径手拼进
    `file:{path}?mode=ro` 会让 SQLite 报 "unable to open database"——
    SQLite 的 URI 解析不处理 `..` 段，Windows 盘符形态 `D:/...` 也必须
    以 `file:///D:/...` 开头才能识别。此处先 `resolve()` 消掉 `..`/`.`
    与符号链接，再用 `Path.as_uri()` 做百分号编码（空格、中文等），
    最后追加 `mode`（默认 ro；immutable=True 时加 `immutable=1`）。
    可写打开（mode='rwc'/'rw'）必须配套 sqlite3.connect(..., uri=True)，
    否则 URI 会被当成字面文件名——且主连接未开 SQLITE_OPEN_URI 时
    ATTACH 的 file: URI 也不按 URI 解析（Windows 编译期默认关）。

    immutable 只保留给"确定不会再变"的场景（如读取盘上副本、比对期间
    的临时物化库）。封库后的 snapshot.db 仍会被就地写（hash /
    build-fts / build-stats），immutable 的"文件永不变化"前提不成立。
    """
    p = Path(path)
    if not p.is_absolute():
        raise ValueError(f"路径必须是绝对路径：{path}（请先 resolve()）")
    p = p.resolve()
    uri = p.as_uri() + f"?mode={mode}"
    if immutable:
        uri += "&immutable=1"
    return uri


def open_snapshot(path: "str | Path", check_same_thread: bool = True) -> sqlite3.Connection:
    """只读打开快照库（mode=ro，普通只读，**不用** immutable=1）。

    为什么不用 immutable=1：immutable 向 SQLite 声明"文件永不变化"，会跳过
    锁与缓存失效校验。但封库后的 snapshot.db 仍会被就地写（hash / build-fts /
    build-stats 回填），前提不成立——写入后旧 immutable 连接读到过期/错乱页，
    表现为 "database disk image is malformed"（文件其实没坏，重启服务即
    "恢复"）。这正是 Windows 实机上那类"假损坏"的根因。
    """
    conn = sqlite3.connect(file_uri(path, immutable=False), uri=True,
                           check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    return conn


def open_catalog(path: "str | Path", check_same_thread: bool = True) -> sqlite3.Connection:
    """打开（必要时创建）catalog 库：WAL + 外键 + 5s busy 超时。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    # WAL 回退（P0-3）：journal_mode 是查询——SQLite 返回实际生效的模式。
    # exFAT/FAT32 等不支持共享内存文件的文件系统上 WAL 会静默失败（返回
    # 原模式，常见为 delete），此时确保为 DELETE 并降级 synchronous。
    # 以返回值为准，不做平台/文件系统探测。
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if mode != "wal":
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=NORMAL")
        logging.getLogger(__name__).warning(
            "catalog 库不支持 WAL（journal_mode=%s，常见于 exFAT/FAT32），"
            "已回退 DELETE 日志模式（synchronous=NORMAL）", mode)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate_catalog(conn: sqlite3.Connection) -> None:
    """幂等列迁移：pragma table_info 检查 + ALTER TABLE ADD COLUMN。

    tasks 表早期 DDL 缺 started_at/progress/message/result_json（P1.0 任务队列需要），
    对既有 catalog.db（如 /home/jack2/cold-data/catalog.db）补列，不影响已注册快照。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if not cols:
        return  # 表不存在（首次建库由 CATALOG_DDL 直接带全列）
    for name, decl in (
        ("started_at", "TEXT"),
        ("progress", "REAL"),
        ("message", "TEXT"),
        ("result_json", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")

    # 昵称列（P1）：disks / volumes 各加 nickname TEXT，幂等
    for table in ("disks", "volumes"):
        tcols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if tcols and "nickname" not in tcols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN nickname TEXT")

    # SSD 专属列（P4-②扩展）：disk_smart 补列，幂等
    sm_cols = {r[1] for r in conn.execute("PRAGMA table_info(disk_smart)")}
    if sm_cols:
        for name, decl in (
            ("life_left_pct", "INTEGER"),
            ("percentage_used", "INTEGER"),
            ("available_spare_pct", "INTEGER"),
            ("written_bytes", "INTEGER"),
            ("read_bytes", "INTEGER"),
            ("media_errors", "INTEGER"),
            ("unsafe_shutdowns", "INTEGER"),
            ("power_cycles", "INTEGER"),
            ("controller_busy_minutes", "INTEGER"),
            ("ssd_json", "TEXT"),
        ):
            if name not in sm_cols:
                conn.execute(f"ALTER TABLE disk_smart ADD COLUMN {name} {decl}")
    conn.commit()


def init_catalog(conn: sqlite3.Connection) -> None:
    """建 catalog 表结构（幂等）+ 补列迁移。"""
    conn.executescript(CATALOG_DDL)
    _migrate_catalog(conn)
    conn.commit()


def init_snapshot(conn: sqlite3.Connection) -> None:
    """建快照库表结构（幂等，仅供测试/工具使用；采集流程在封库期建索引）。"""
    conn.executescript(SNAPSHOT_DDL)
    conn.commit()
