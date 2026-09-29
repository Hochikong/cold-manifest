"""diff 物化结果 → 自包含 HTML 报告（§6.2 format=html / §7.3 导出/报告）。

只读打开 diff 结果库（<data_root>/_diffs/<diff_id>.db），不依赖 catalog：
A/B 快照采集时间等元信息由调用方以 snapshot_meta 传入（可选）。
自包含 = 内联 CSS、无外部资源、无 JS；折叠用 <details>/<summary>。
所有动态文本一律 html.escape（路径可含 <、&、引号、中文）。
"""

import html
import io
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

# 与 diff_engine._COUNTED / routes_diffs._CATEGORIES 一致；identical 不落库，只进摘要
_SECTION_CATEGORIES = ("added", "removed", "type_changed", "size_changed",
                       "mtime_changed", "content_changed", "moved_or_renamed")

_CSS = """
body{font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;margin:0;
     background:#f5f6f8;color:#1f2328}
main{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:20px} h2{font-size:16px;margin:28px 0 8px}
.cards{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}
.card{background:#fff;border:1px solid #d8dbe0;border-radius:8px;padding:10px 14px;
      min-width:120px}
.card .num{font-size:22px;font-weight:600}
.card .lbl{font-size:12px;color:#57606a}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border:1px solid #d8dbe0;padding:5px 9px;text-align:left}
th{background:#eef0f3}
td.num,th.num{text-align:right}
.pos{color:#0a7d32}.neg{color:#c62828}
.muted{color:#57606a}
details{background:#fff;border:1px solid #d8dbe0;border-radius:8px;
        padding:8px 14px;margin:10px 0}
summary{cursor:pointer;font-weight:600;font-size:14px}
.truncated{margin:6px 0;color:#9a6700;font-size:13px}
.meta td{border:none;padding:2px 12px 2px 0;font-size:13px}
.meta td:first-child{color:#57606a}
"""


def fmt_bytes(n: "int | float | None") -> str:
    """人读字节单位；None → '—'。"""
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024 or unit == "PiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PiB"


def _fmt_ns(ns: "int | None") -> str:
    if ns is None:
        return "—"
    try:
        return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)\
            .strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return str(ns)


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _open_ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parent_dir_expr() -> str:
    """父目录表达式（与 routes_diffs.summary 相同写法：剥末段与尾部 '/'）。"""
    return ("CASE WHEN instr(path, '/') = 0 THEN '(root)' "
            "ELSE rtrim(rtrim(path, replace(path, '/', '')), '/') END")


def _load_meta(conn: sqlite3.Connection) -> dict:
    meta = {r[0]: r[1] for r in
            conn.execute("SELECT key, value FROM diff_meta")}
    summary = {}
    try:
        summary = json.loads(meta.get("summary_json", "{}"))
    except json.JSONDecodeError:
        pass
    try:
        options = json.loads(meta.get("options_json", "{}"))
    except json.JSONDecodeError:
        options = {}
    return {"raw": meta, "summary": summary, "options": options}


def _table(headers: "list[str]", rows: "list[list[str]]",
           num_cols: "set[int]" = frozenset()) -> "list[str]":
    out = ["<table><thead><tr>"]
    for i, h in enumerate(headers):
        cls = ' class="num"' if i in num_cols else ""
        out.append(f"<th{cls}>{_esc(h)}</th>")
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>")
        for i, cell in enumerate(row):
            cls = ' class="num"' if i in num_cols else ""
            out.append(f"<td{cls}>{cell}</td>")   # cell 已由调用方转义
        out.append("</tr>")
    out.append("</tbody></table>")
    return out


def generate_diff_report(diff_db_path: "str | Path", out: "str | Path | IO[str]",
                         *, max_rows_per_section: int = 1000,
                         snapshot_meta: "dict | None" = None) -> dict:
    """生成自包含 HTML 报告并写入 out（路径或文本流），返回摘要统计。

    snapshot_meta（可选）：{"a": {...}, "b": {...}}，每侧可含 collected_at /
    file_count / total_bytes 等，直接进头部；不传则只展示快照 ID。
    """
    t0 = time.monotonic()
    db = Path(diff_db_path)
    if not db.is_file():
        raise FileNotFoundError(f"diff 结果库不存在：{db}")
    conn = _open_ro(db)
    try:
        meta = _load_meta(conn)
        raw, summary = meta["raw"], meta["summary"]

        # 库内实数为准（不信任 summary_json）
        counts: "dict[str, int | None]" = {}
        for cat in (*_SECTION_CATEGORIES, "identical"):
            row = conn.execute(
                "SELECT COUNT(*) FROM diff_entries WHERE change_type=?",
                (cat,)).fetchone()
            counts[cat] = row[0] if cat != "identical" else row[0] or None
            if cat == "identical" and not counts[cat]:
                counts[cat] = summary.get("identical")  # 不落库：用物化时计数
        total_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(COALESCE(b_size,0)-COALESCE(a_size,0)),0)"
            " FROM diff_entries").fetchone()
        total_changes, total_size_delta = int(total_row[0]), int(total_row[1])

        # 目录 Top：净大小变化排序
        dir_top = conn.execute(
            f"""
            SELECT {_parent_dir_expr()} AS pdir, COUNT(*) AS n,
                   SUM(COALESCE(b_size,0)-COALESCE(a_size,0)) AS size_delta
            FROM diff_entries GROUP BY pdir
            ORDER BY size_delta DESC, pdir LIMIT 200
            """).fetchall()

        # 各分类清单（截断）
        sections: "dict[str, dict]" = {}
        for cat in _SECTION_CATEGORIES:
            n = counts[cat] or 0
            if n == 0:
                continue
            rows = conn.execute(
                "SELECT path, a_type, b_type, a_size, b_size,"
                " a_mtime_ns, b_mtime_ns FROM diff_entries"
                " WHERE change_type=? ORDER BY path LIMIT ?",
                (cat, max_rows_per_section)).fetchall()
            sections[cat] = {"total": n, "rows": [dict(r) for r in rows]}
    finally:
        conn.close()

    a_id, b_id = raw.get("a", "?"), raw.get("b", "?")
    sm = snapshot_meta or {}
    gen_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    h: "list[str]" = ["<!DOCTYPE html>", '<html lang="zh-CN">', "<head>",
                      '<meta charset="utf-8">',
                      f"<title>diff 报告 { _esc(a_id) } ↔ { _esc(b_id) }</title>",
                      f"<style>{_CSS}</style>", "</head>", "<body><main>"]
    h.append(f"<h1>冷备份对比报告 <span class='muted'>{_esc(db.stem)}</span></h1>")

    # 头部元信息
    meta_rows = [
        ["快照 A", _esc(a_id) + _meta_suffix(sm.get("a"))],
        ["快照 B", _esc(b_id) + _meta_suffix(sm.get("b"))],
        ["参数", _esc(json.dumps(meta["options"], ensure_ascii=False, sort_keys=True))],
        ["diff 生成时间", _esc(raw.get("created_at", "—"))],
        ["报告生成时间", _esc(gen_at)],
    ]
    h.append("<table class='meta'>")
    for k, v in meta_rows:
        h.append(f"<tr><td>{_esc(k)}</td><td>{v}</td></tr>")
    h.append("</table>")

    # 摘要卡片
    h.append("<h2>变更摘要</h2><div class='cards'>")
    cat_labels = {"added": "新增", "removed": "丢失", "type_changed": "类型变化",
                  "size_changed": "大小变化", "mtime_changed": "mtime 变化",
                  "content_changed": "内容变化", "moved_or_renamed": "移动/改名",
                  "identical": "完全一致"}
    order = ("added", "removed", "type_changed", "size_changed", "mtime_changed",
             "content_changed", "moved_or_renamed", "identical")
    for cat in order:
        v = counts.get(cat)
        shown = "—" if v is None else f"{v:,}"
        h.append(f"<div class='card'><div class='num'>{_esc(shown)}</div>"
                 f"<div class='lbl'>{_esc(cat_labels[cat])}</div></div>")
    cls = "pos" if total_size_delta > 0 else ("neg" if total_size_delta < 0 else "")
    h.append(f"<div class='card'><div class='num {cls}'>"
             f"{_esc('+' if total_size_delta > 0 else '')}{_esc(fmt_bytes(total_size_delta))}</div>"
             f"<div class='lbl'>净大小变化</div></div>")
    h.append("</div>")

    # 目录变化 Top
    h.append("<h2>目录变化 Top（按净大小变化）</h2>")
    rows = []
    for r in dir_top:
        sd = int(r["size_delta"] or 0)
        sign = "+" if sd > 0 else ""
        sdc = f"<span class='{ 'pos' if sd > 0 else ('neg' if sd < 0 else 'muted') }'>"\
              f"{_esc(sign)}{_esc(fmt_bytes(sd))}</span>"
        rows.append([_esc(r["pdir"]), _esc(f"{r['n']:,}"), sdc])
    h += _table(["目录", "变更条数", "净大小变化"], rows, num_cols={1, 2})

    # 各分类清单
    sec_labels = {"added": "新增（仅 B）", "removed": "丢失（仅 A）",
                  "type_changed": "类型变化", "size_changed": "大小变化",
                  "mtime_changed": "mtime 变化", "content_changed": "内容变化",
                  "moved_or_renamed": "移动/改名"}
    has_type_diff = any(s["rows"] and (r["a_type"] != r["b_type"])
                        for s in sections.values() for r in s["rows"])
    headers = (["路径", "类型", "A 大小", "B 大小", "A mtime", "B mtime"]
               if has_type_diff else
               ["路径", "A 大小", "B 大小", "A mtime", "B mtime"])
    num_cols = set(range(1, len(headers)))
    for cat, sec in sections.items():
        h.append(f"<details open><summary>{_esc(sec_labels[cat])}"
                 f"（{sec['total']:,} 条）</summary>")
        rows = []
        for r in sec["rows"]:
            cells = [_esc(r["path"])]
            if has_type_diff:
                t = _esc(r["a_type"] or "—") if r["a_type"] == r["b_type"] \
                    else f"{_esc(r['a_type'] or '—')} → {_esc(r['b_type'] or '—')}"
                cells.append(t)
            delta = (r["b_size"] or 0) - (r["a_size"] or 0) \
                if r["a_size"] is not None and r["b_size"] is not None else None
            if delta is not None and delta and cat == "size_changed":
                dc = "pos" if delta > 0 else "neg"
                cells.append(f"<span class='{dc}'>{_esc(fmt_bytes(r['a_size']))}</span>")
                cells.append(f"<span class='{dc}'>{_esc(fmt_bytes(r['b_size']))}</span>")
            else:
                cells.append(_esc(fmt_bytes(r["a_size"])))
                cells.append(_esc(fmt_bytes(r["b_size"])))
            cells.append(_esc(_fmt_ns(r["a_mtime_ns"])))
            cells.append(_esc(_fmt_ns(r["b_mtime_ns"])))
            rows.append(cells)
        h += _table(headers, rows, num_cols=num_cols)
        if sec["total"] > len(sec["rows"]):
            h.append(f"<p class='truncated'>共 {sec['total']:,} 条，"
                     f"已截断（仅显示前 {len(sec['rows']):,} 条）</p>")
        h.append("</details>")

    h.append(f"<p class='muted'>生成耗时 "
             f"{time.monotonic() - t0:.2f}s · cold-manifest</p>")
    h.append("</main></body></html>")
    doc = "\n".join(h)

    if isinstance(out, (str, Path)):
        Path(out).write_text(doc, encoding="utf-8")
    else:
        assert isinstance(out, io.TextIOBase) or hasattr(out, "write")
        out.write(doc)

    return {
        "diff_id": db.stem,
        "a": a_id,
        "b": b_id,
        "counts": counts,
        "total_changes": total_changes,
        "total_size_delta": total_size_delta,
        "sections": {c: s["total"] for c, s in sections.items()},
        "generated_at": gen_at,
        "elapsed_s": round(time.monotonic() - t0, 3),
    }


def _meta_suffix(m: "dict | None") -> str:
    """快照采集时间等后缀（已转义拼接）。"""
    if not m:
        return ""
    parts = []
    if m.get("collected_at"):
        parts.append(f"采集 {_esc(m['collected_at'])}")
    if m.get("total_bytes") is not None:
        parts.append(f"{_esc(fmt_bytes(m['total_bytes']))}")
    return f"<span class='muted'>（{' · '.join(parts)}）</span>" if parts else ""
