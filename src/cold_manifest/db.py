"""SQLite 连接辅助：快照库只读打开，catalog 可写（WAL）。"""

import sqlite3
from pathlib import Path

from .schema import CATALOG_DDL, SNAPSHOT_DDL


def file_uri(path: "str | Path", *, immutable: bool = True) -> str:
    """构造只读打开 SQLite 库的 file: URI（全项目唯一构造点）。

    为什么必须统一走这里：Windows 上数据根常形如 `D:\\pkg\\bin\\..\\..\\data`
    （start.cmd 的 %~dp0 展开），直接把未归一化路径手拼进
    `file:{path}?mode=ro` 会让 SQLite 报 "unable to open database"——
    SQLite 的 URI 解析不处理 `..` 段，Windows 盘符形态 `D:/...` 也必须
    以 `file:///D:/...` 开头才能识别。此处先 `resolve()` 消掉 `..`/`.`
    与符号链接，再用 `Path.as_uri()` 做百分号编码（空格、中文等），
    最后追加 `mode=ro`（immutable=True 时加 `immutable=1`）。
    """
    p = Path(path)
    if not p.is_absolute():
        raise ValueError(f"路径必须是绝对路径：{path}（请先 resolve()）")
    p = p.resolve()
    uri = p.as_uri() + "?mode=ro"
    if immutable:
        uri += "&immutable=1"
    return uri


def open_snapshot(path: "str | Path", check_same_thread: bool = True) -> sqlite3.Connection:
    """只读打开快照库。

    immutable=1 声明文件不再变化：跳过锁与缓存校验，只读查询最快
    （封库后的 snapshot.db 是不可变制品）。
    """
    conn = sqlite3.connect(file_uri(path, immutable=True), uri=True,
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
