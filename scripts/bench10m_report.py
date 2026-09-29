#!/usr/bin/env python3
"""10M diff 报告生成计时：generate_diff_report 耗时 / 产物大小 / dir_top GROUP BY 单独计时。

用法：python scripts/bench10m_report.py <data_root>
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cold_manifest.report import generate_diff_report  # noqa: E402

DATA_ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/bench10m")
DIFF_DB = Path(sys.argv[2]) if len(sys.argv) > 2 else None

if DIFF_DB is None:
    # 取最新的 sha256 diff 库（summary 含 content_changed 的）
    cat = sqlite3.connect((DATA_ROOT / "catalog.db").as_posix())
    row = cat.execute("SELECT result_path FROM diff_runs WHERE options_json LIKE '%sha256%'"
                      " ORDER BY created_at DESC LIMIT 1").fetchone()
    cat.close()
    DIFF_DB = Path(row[0])

# dir_top 的 GROUP BY 单独计时（报告内同款 SQL）
c = sqlite3.connect(f"file:{DIFF_DB.as_posix()}?mode=ro", uri=True)
expr = ("CASE WHEN instr(path, '/') = 0 THEN '(root)' "
        "ELSE rtrim(rtrim(path, replace(path, '/', '')), '/') END")
t = time.monotonic()
rows = c.execute(f"SELECT {expr} AS pdir, COUNT(*) n,"
                 " SUM(COALESCE(b_size,0)-COALESCE(a_size,0)) sd FROM diff_entries"
                 " GROUP BY pdir ORDER BY sd DESC, pdir LIMIT 200").fetchall()
dir_top_s = time.monotonic() - t
c.close()

out = DATA_ROOT / "bench_report.html"
t = time.monotonic()
meta = generate_diff_report(DIFF_DB, out)
el = time.monotonic() - t
res = {
    "diff_db": str(DIFF_DB),
    "dir_top_group_by_s": round(dir_top_s, 3),
    "dir_top_rows": len(rows),
    "generate_s": round(el, 3),
    "html_bytes": out.stat().st_size,
    "counts": meta["counts"],
}
(DATA_ROOT / "report_timings.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
print(json.dumps(res, indent=1, ensure_ascii=False))
