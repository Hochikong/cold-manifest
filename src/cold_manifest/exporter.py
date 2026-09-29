"""导出实现（CLI `cldm export` 与 Web API 共用）。

- export_csv：entries 全表 → 单 CSV（流式 fetchmany，不整表载入）。
- export_v1_csv：快照库 → v1-layout 三件套 metadata/tree/warnings.csv，
  格式可被 legacy `backuptools compare` 直接消费。
- export_v1_csv_zip：三件套打进临时 zip（API 下载用），调用方负责清理。
"""

import csv
import io
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

_DIFF_CSV_HEADER = ["category", "path", "type", "size_a", "size_b", "mtime_a", "mtime_b"]

_FETCH = 8192


def ns_to_iso(ns: "int | None") -> str:
    """ns 整数 → ISO UTC（秒级，'2026-05-04T12:09:03Z'）；无值返回空串。"""
    if not ns:
        return ""
    secs, _ = divmod(int(ns), 1_000_000_000)
    return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_snapshot_rwcheck(data_root: "str | Path", snapshot_id: str) -> sqlite3.Connection:
    """按快照 ID（或直接给 .db 路径）只读打开快照库；不存在抛 FileNotFoundError。"""
    from .catalog import snapshot_path
    from .db import open_snapshot

    db = Path(snapshot_id)
    if not db.suffix and not db.is_file():
        db = snapshot_path(data_root, snapshot_id)
        if not db.is_file():
            raise FileNotFoundError(f"快照不存在：{snapshot_id}（{db}）")
    return open_snapshot(db)


def export_csv(conn: sqlite3.Connection, out: Path) -> None:
    """entries 全表按 entry_id 序导出单 CSV，表头与 entries 列一致。"""
    cur = conn.execute("SELECT * FROM entries ORDER BY entry_id")
    cols = [d[0] for d in cur.description]
    with Path(out).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        while True:
            rows = cur.fetchmany(_FETCH)
            if not rows:
                break
            w.writerows(tuple(r) for r in rows)


def export_v1_csv(conn: sqlite3.Connection, out_dir: Path) -> None:
    """导出 v1-layout 三件套到 out_dir（须已存在或可创建）。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_rows = conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    meta = {r[0]: r[1] for r in meta_rows}
    volume_id = meta.get("volume_id") or ""
    max_depth = conn.execute("SELECT COALESCE(MAX(depth), 1) FROM entries").fetchone()[0]

    # metadata.csv：全量 meta 行（import-legacy 兼容多余键）
    with (out_dir / "metadata.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["metric_name", "value"])
        for r in meta_rows:
            w.writerow([r[0], r[1]])

    # tree.csv：动态 level_1..N 列；只含 v1 同构行：file + 无子项目的录（empty_dir）。
    # 非空目录在 v1 中由 scanner 按路径推导、不写行，导出也必须省略，
    # 否则 legacy compare 会对每个非空目录报一行 type_diff 噪声。
    header = ["volume_id", "relative_path", "entry_name", "entry_type",
              "size_bytes", "modified_utc"] + [f"level_{i}" for i in range(1, max_depth + 1)]
    cur = conn.execute(
        "SELECT path, name, type, size_bytes, mtime_ns FROM entries "
        "WHERE entry_id != 1 AND "
        "(type = 'file' OR (type = 'dir' AND "
        "entry_id NOT IN (SELECT parent_id FROM entries))) ORDER BY path")
    with (out_dir / "tree.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        while True:
            rows = cur.fetchmany(_FETCH)
            if not rows:
                break
            for path, name, etype, size, mtime in rows:
                parts = path.split("/")
                levels = parts + [""] * (max_depth - len(parts))
                is_file = etype == "file"
                w.writerow([volume_id, path, name,
                            "file" if is_file else "empty_dir",
                            size if is_file else "0",
                            ns_to_iso(mtime), *levels])

    # warnings.csv：表头 stage,path,warning_type,detail（skipped 表列序不同，显式映射）
    with (out_dir / "warnings.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stage", "path", "warning_type", "detail"])
        for stage, path, wtype, detail in conn.execute(
                "SELECT stage, path, warning_type, detail FROM skipped"):
            w.writerow([stage, path, wtype, detail or ""])


def export_v1_csv_zip(conn: sqlite3.Connection, snapshot_id: str) -> Path:
    """三件套写入临时 zip（外部下载用），返回路径；调用方负责 unlink 清理。"""
    fd, tmp_name = tempfile.mkstemp(prefix=f"{snapshot_id.replace('/', '_')}_v1_", suffix=".zip")
    os.close(fd)
    tmp = Path(tmp_name)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        with tempfile.TemporaryDirectory() as td:
            export_v1_csv(conn, Path(td))
            for name in ("metadata.csv", "tree.csv", "warnings.csv"):
                zf.write(Path(td) / name, arcname=name)
    return tmp


def iter_snapshot_csv(conn: sqlite3.Connection):
    """流式产出快照 CSV 文本块（表头 + fetchmany 批次），供 API StreamingResponse。"""
    cur = conn.execute("SELECT * FROM entries ORDER BY entry_id")
    cols = [d[0] for d in cur.description]
    buf = io.StringIO()
    csv.writer(buf).writerow(cols)
    yield buf.getvalue()
    while True:
        rows = cur.fetchmany(_FETCH)
        if not rows:
            return
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerows(tuple(r) for r in rows)
        yield buf.getvalue()
