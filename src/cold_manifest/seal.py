"""快照库封库共享逻辑：建索引 → dir_rollup → optimize。

import_legacy（CSV 导入）与 collect（在线采集）共用；库表结构见 schema.SNAPSHOT_TABLES_DDL，
封库假设 entries/skipped 已灌完、meta 已写（status=sealed 由调用方在封库前写入）。
"""

import sqlite3

from .schema import SNAPSHOT_INDEXES_DDL


def build_dir_rollup(conn: sqlite3.Connection) -> None:
    """dir_rollup：每个目录的**后代**统计（不含自身），root 覆盖全库。

    做法：先按直接子项聚合，再按深度自底向上合并子目录的聚合值。
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
    conn.execute(
        """
        INSERT OR REPLACE INTO dir_rollup(entry_id, file_count, dir_count, total_bytes, total_allocated, max_mtime_ns)
        SELECT e.entry_id, COALESCE(a.fc, 0), COALESCE(a.dc, 0), COALESCE(a.tb, 0), COALESCE(a.ta, 0), a.mm
        FROM entries e LEFT JOIN agg a ON a.entry_id = e.entry_id
        WHERE e.type = 'dir'
        """
    )
    conn.execute("DROP TABLE agg")


def seal_snapshot(conn: sqlite3.Connection) -> None:
    """封库收尾：建索引 → dir_rollup → PRAGMA optimize → commit。"""
    conn.executescript(SNAPSHOT_INDEXES_DDL)
    build_dir_rollup(conn)
    conn.execute("PRAGMA optimize")
    conn.commit()
