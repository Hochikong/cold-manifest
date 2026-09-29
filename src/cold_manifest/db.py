"""SQLite 连接辅助：快照库只读打开，catalog 可写（WAL）。"""

import sqlite3
from pathlib import Path

from .schema import CATALOG_DDL, SNAPSHOT_DDL


def open_snapshot(path: "str | Path", check_same_thread: bool = True) -> sqlite3.Connection:
    """只读打开快照库。

    immutable=1 声明文件不再变化：跳过锁与缓存校验，只读查询最快
    （封库后的 snapshot.db 是不可变制品）。
    """
    p = Path(path).resolve().as_posix()
    conn = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True,
                           check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    return conn


def open_catalog(path: "str | Path", check_same_thread: bool = True) -> sqlite3.Connection:
    """打开（必要时创建）catalog 库：WAL + 外键 + 5s busy 超时。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
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
