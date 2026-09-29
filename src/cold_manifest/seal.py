"""快照库封库共享逻辑：建索引 → dir_rollup → optimize → FTS5。

import_legacy（CSV 导入）与 collect（在线采集）共用；库表结构见 schema.SNAPSHOT_TABLES_DDL，
封库假设 entries/skipped 已灌完、meta 已写（status=sealed 由调用方在封库前写入）。
"""

import sqlite3
from pathlib import Path
from typing import Callable

from .schema import SNAPSHOT_INDEXES_DDL


# FTS5 全文索引（contentless：只存分词后的倒排索引，原文以 entries 为准，rowid=entry_id）。
# trigram 分词支持 CJK 与任意子串匹配（需 SQLite ≥3.34）；不支持时回退 unicode61（按词匹配）。
ENTRIES_FTS_COLUMNS = ("name", "path")


def fts_tokenizer(conn: sqlite3.Connection) -> str:
    """探测当前 SQLite 是否支持 trigram 分词；不支持则回退 unicode61。"""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts_probe USING fts5(a, tokenize='trigram')")
        conn.execute("DROP TABLE temp._fts_probe")
        return "trigram"
    except sqlite3.OperationalError:
        return "unicode61"


def fts_available(conn: sqlite3.Connection) -> bool:
    """快照库是否已建 entries_fts 全文索引。"""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries_fts'"
    ).fetchone() is not None


def build_fts(conn: sqlite3.Connection) -> tuple[int, str]:
    """为快照库构建/重建 entries_fts（幂等：已存在则 DROP 后重建）。

    返回 (条目数, 分词器)。contentless FTS5 只存倒排索引，查询时按 rowid 回
    JOIN entries 取原文，体积开销约为 path+name 文本的 10–20%。
    快照库是不可变制品：重建（而非增量）保证索引与 entries 严格一致。
    """
    tok = fts_tokenizer(conn)
    if fts_available(conn):
        conn.execute("DROP TABLE entries_fts")
    conn.execute(
        f"CREATE VIRTUAL TABLE entries_fts USING fts5("
        f"name, path, content='', tokenize='{tok}')"
    )
    conn.execute(
        "INSERT INTO entries_fts(rowid, name, path) SELECT entry_id, name, path FROM entries"
    )
    count = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    return count, tok


def fts_match_query(q: str) -> str:
    """用户输入转为 FTS5 短语查询：内部双引号翻倍后整体外包双引号。

    短词语法下输入被当作字面子串（trigram 分词时即子串匹配），
    不会把用户的 * ? : 等字符解释为 MATCH 语法。
    """
    return '"' + q.replace('"', '""') + '"'

def build_dir_rollup(conn: sqlite3.Connection, step_done: "Callable[[], None] | None" = None) -> None:
    """dir_rollup：每个目录的**后代**统计（不含自身），root 覆盖全库。

    做法：先按直接子项聚合，再按深度自底向上合并子目录的聚合值。
    step_done：可选回调，每完成一个 depth 合并步调用一次（封库进度粒度）。
    """
    conn.execute(
        """
        CREATE TEMP TABLE agg(
          entry_id INTEGER PRIMARY KEY,
          fc INTEGER NOT NULL, dc INTEGER NOT NULL,
          tb INTEGER NOT NULL, ta INTEGER NOT NULL, mm INTEGER
        )
        """
    )
    # 初始：每个目录的直接子文件 + 子目录占位（子目录聚合后续逐层并入）
    conn.execute(
        """
        INSERT INTO agg(entry_id, fc, dc, tb, ta, mm)
        SELECT d.entry_id,
               COALESCE(SUM(CASE WHEN e.type='file' THEN 1 END), 0),
               COALESCE(SUM(CASE WHEN e.type='dir' THEN 1 END), 0),
               COALESCE(SUM(CASE WHEN e.type='file' THEN e.size_bytes END), 0),
               COALESCE(SUM(CASE WHEN e.type='file' THEN e.allocated_bytes END), 0),
               MAX(CASE WHEN e.type='file' THEN e.mtime_ns END)
        FROM entries d JOIN entries e ON e.parent_id = d.entry_id
        WHERE d.type = 'dir'
        GROUP BY d.entry_id
        """
    )
    max_depth = conn.execute("SELECT COALESCE(MAX(depth), 0) FROM entries WHERE type='dir'").fetchone()[0]
    for depth in range(max_depth, -1, -1):
        # 注意：SQLite 的标量 MAX(a,b) 任一为 NULL 即返回 NULL，mtime 合并须显式 CASE
        conn.execute(
            """
            UPDATE agg SET
              fc = fc + COALESCE(c.sfc, 0),
              dc = dc + COALESCE(c.sdc, 0),
              tb = tb + COALESCE(c.stb, 0),
              ta = ta + COALESCE(c.sta, 0),
              mm = CASE WHEN mm IS NULL THEN c.smm
                        WHEN c.smm IS NULL THEN mm
                        ELSE MAX(mm, c.smm) END
            FROM (SELECT e.parent_id AS pid,
                         SUM(c.fc) AS sfc, SUM(c.dc) AS sdc,
                         SUM(c.tb) AS stb, SUM(c.ta) AS sta, MAX(c.mm) AS smm
                  FROM entries e JOIN agg c ON c.entry_id = e.entry_id
                  GROUP BY e.parent_id) c
            WHERE agg.entry_id = c.pid
              AND agg.entry_id IN (SELECT entry_id FROM entries WHERE type='dir' AND depth = ?)
            """,
            (depth,),
        )
        if step_done is not None:
            step_done()
    conn.execute(
        """
        INSERT OR REPLACE INTO dir_rollup(entry_id, file_count, dir_count, total_bytes, total_allocated, max_mtime_ns)
        SELECT e.entry_id, COALESCE(a.fc, 0), COALESCE(a.dc, 0), COALESCE(a.tb, 0), COALESCE(a.ta, 0), a.mm
        FROM entries e LEFT JOIN agg a ON a.entry_id = e.entry_id
        WHERE e.type = 'dir'
        """
    )
    conn.execute("DROP TABLE agg")


def seal_snapshot(conn: sqlite3.Connection,
                  progress: "Callable[[int, int], None] | None" = None) -> None:
    """封库收尾：建索引 → dir_rollup → PRAGMA optimize → FTS5 → commit。

    progress(done, total)：细粒度步骤——建索引 1 步、dir_rollup 按 depth
    多步（max_depth+1）、optimize 1 步、FTS5 1 步，total = max_depth + 4。
    FTS 放在 PRAGMA optimize 之后：optimize 面向普通索引/统计信息，
    全文索引是封库末尾的一次性批量构建，无需（也不宜）参与 optimize。
    """
    max_depth = conn.execute(
        "SELECT COALESCE(MAX(depth), 0) FROM entries WHERE type='dir'").fetchone()[0]
    total = max_depth + 4
    done = 0

    def tick() -> None:
        nonlocal done
        done += 1
        if progress is not None:
            progress(done, total)

    conn.executescript(SNAPSHOT_INDEXES_DDL)
    tick()
    build_dir_rollup(conn, step_done=tick)
    conn.execute("PRAGMA optimize")
    tick()
    build_fts(conn)
    tick()
    conn.commit()


def is_collect_orphan(ts_dir: Path) -> bool:
    """ts 目录是否为本工具采集产生的未封库孤儿（可安全删除）。

    tasks.TaskRunner 启动清扫与 collect._sweep_leftovers 共用同一判定口径：
    - snapshot.db 缺失 → 建库前崩溃残留，孤儿；
    - snapshot.db 存在 → 须带 collector_version meta（确认采集产生）且未 sealed；
      打不开/缺 meta 的库保守起见不动（可能是外部/测试数据）；
    - 带 scan_journal.jsonl 且未 sealed → **可续采（P1.2b 断点续采），保留**，
      不再视为孤儿；续采完成（封库）或无 journal 的残留下仍照旧清理。
    """
    db = ts_dir / "snapshot.db"
    if not db.is_file():
        return True
    resumable = (ts_dir / "scan_journal.jsonl").is_file()
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        keys = {r[0] for r in conn.execute("SELECT key FROM meta").fetchall()}
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    if "collector_version" not in keys:
        return False
    sealed = is_sealed(db)
    if resumable and not sealed:
        return False  # 可续采：有 journal 的未封库目录，清扫保留
    return not sealed


def is_sealed(db_path) -> bool:
    """快照库是否已封库（meta 中存在 status=sealed）。库缺失/损坏 → False。"""
    try:
        conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='status'").fetchone()
        return bool(row) and row[0] == "sealed"
    except sqlite3.Error:
        return False
    finally:
        conn.close()
