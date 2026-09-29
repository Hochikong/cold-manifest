"""快照内重复文件报告（P3-A）：按完整哈希分组，量化重复浪费的空间。

只认完整哈希：读快照 meta 的 hash_policy，非 'full' → DuplicatesError
（CLI 退出码 2 / API 400），提示先跑 `cldm hash <sid> --policy full`。
逐条目 policy 未落库，混跑情形从严，一律以 meta 为准。

性能（本项目经验教训，见 diff_engine._materialize_sql）：SQLite 随机行读在
HDD/WSL-VHD 上约 43µs/条，逐行探测会退化。此处全部走顺序扫：
- 分组聚合 GROUP BY hash_hex 走 idx_entries_hash（partial 索引，index-only）；
- 每组路径取样用 TEMP 表 + 窗口函数（ROW_NUMBER），一次顺序扫出前 20 条路径；
- 分页 keyset（wasted_bytes DESC, size_bytes DESC, hash_hex ASC），禁用 OFFSET。
  排序键是三元组合，pagination.encode_cursor 只支持单键，故自定义游标编码。

自包含：只依赖快照库（entries/meta），不依赖 catalog。
"""

import base64
import binascii
import csv
import html
import io
import json
import sqlite3
from typing import Any

from .report import _CSS, fmt_bytes

# 每组最多返回的路径数；超出置 paths_truncated=true（稳定：按 path 排序取前 N）
PATHS_PER_GROUP = 20

DEFAULT_MIN_SIZE = 1048576  # 1 MiB：重复报告默认忽略小文件
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


class DuplicatesError(Exception):
    """重复文件报告无法生成（策略非 full / 无哈希 / 游标非法）。"""


# ---------------------------------------------------------------- 游标编码
# 组合排序键 (wasted_bytes DESC, size_bytes DESC, hash_hex ASC)，三元组无法塞进
# pagination.encode_cursor 的单键形式，这里自定义同风格的不透明 base64url 编码。


def encode_dup_cursor(wasted: int, size: int, hash_hex: str) -> str:
    payload = json.dumps({"w": wasted, "s": size, "h": hash_hex},
                         ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_dup_cursor(cursor: str) -> "tuple[int, int, str]":
    """解析游标 → (wasted, size, hash_hex)；非法编码抛 ValueError。"""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        obj = json.loads(raw)
        w, s, h = obj["w"], obj["s"], obj["h"]
        if not (isinstance(w, int) and isinstance(s, int) and isinstance(h, str)):
            raise ValueError("cursor 字段类型错误")
        return w, s, h
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError,
            KeyError, TypeError, ValueError) as e:
        raise ValueError(f"非法 cursor：{e}") from e


# ---------------------------------------------------------------- 核心

def _require_full_hash(conn: sqlite3.Connection) -> str:
    """meta.hash_policy 必须为 'full'，返回 hash_algo；否则抛 DuplicatesError。"""
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    policy = meta.get("hash_policy")
    if policy != "full":
        raise DuplicatesError(
            f"该快照哈希策略不是 full（hash_policy={policy or '未设置'}），"
            "重复报告只认完整哈希；请先运行：cldm hash <snapshot_id> --policy full")
    return meta.get("hash_algo") or "sha256"


def find_duplicates(
    conn: sqlite3.Connection,
    snapshot_id: str,
    *,
    min_size: int = DEFAULT_MIN_SIZE,
    limit: int = DEFAULT_LIMIT,
    cursor: "str | None" = None,
) -> dict:
    """按完整哈希分组找出重复文件，返回报告 dict（API/CLI 共用形状）。

    顺序扫两遍（分组聚合 + hashed_files 计数），再加一遍受限的路径取样。
    conn 为只读 immutable 连接亦可（TEMP 表写在独立 temp 库，不受影响）。
    """
    if min_size < 0:
        raise DuplicatesError("min_size 不能为负")
    if limit < 1:
        raise DuplicatesError("limit 不能小于 1")

    hash_algo = _require_full_hash(conn)

    # 游标 → 排序键下界之后：keyset 条件（w, s, h) 均为上一页末组的键。
    # 条件引用聚合结果（wasted/sz），只能放在外层 WHERE（对 CTE 输出过滤）。
    cursor_cond = ""
    bind: "dict[str, Any]" = {"min": min_size, "limit": limit + 1}
    if cursor is not None:
        w, s, h = decode_dup_cursor(cursor)  # ValueError 由路由/CLI 转 400/退出 2
        # sqlite3 不支持命名+位置混绑，keyset 条件同样用命名参数
        cursor_cond = (" AND (wasted < :c0 OR (wasted = :c1 AND sz < :c2)"
                       " OR (wasted = :c3 AND sz = :c4 AND hash_hex > :c5))")
        bind.update(c0=w, c1=w, c2=s, c3=w, c4=s, c5=h)

    # 单趟顺序扫（本项目教训：随机行读是 HDD/WSL-VHD 的硬墙，见
    # diff_engine._materialize_sql——全部 IO 压成一趟表扫，不做逐组随机探测）。
    # scanned 按 hash_hex 聚合一次，min_size 下推进聚合条件；
    # wasted = 组内字节和 − 保留一份（最小拷贝）；full 哈希同组必同内容同大小，
    # 等价于 size*(count-1)。总量/已哈希数用标量子查询复用物化 CTE，
    # 不再二次扫表；多取 1 行判断 has_more，避免"恰好取满 → 空尾页"。
    sql = """
    WITH scanned AS MATERIALIZED (
      SELECT hash_hex,
             COUNT(*) AS n_hashed,
             SUM(size_bytes >= :min) AS n_in,
             SUM(CASE WHEN size_bytes >= :min THEN size_bytes END) AS bytes_in,
             MIN(CASE WHEN size_bytes >= :min THEN size_bytes END) AS keep
      FROM entries
      WHERE type='file' AND hash_hex IS NOT NULL AND hash_hex != ''
      GROUP BY hash_hex)
    SELECT hash_hex, n_in AS cnt, keep AS sz, bytes_in - keep AS wasted,
           (SELECT CAST(SUM(n_hashed) AS INTEGER) FROM scanned),
           (SELECT COUNT(*) FROM scanned WHERE n_in > 1),
           (SELECT CAST(COALESCE(SUM(bytes_in - keep), 0) AS INTEGER)
            FROM scanned WHERE n_in > 1)
    FROM scanned
    WHERE n_in > 1{cursor}
    ORDER BY wasted DESC, sz DESC, hash_hex ASC
    LIMIT :limit""".format(cursor=cursor_cond)
    rows = conn.execute(sql, bind).fetchall()
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    items = [{"hash_hex": r[0], "size_bytes": r[2], "count": r[1],
              "wasted_bytes": r[3]} for r in rows]
    if rows:
        hashed_files, n_groups, total_wasted = rows[0][4], rows[0][5], rows[0][6]
    if not rows:
        # 空页（无重复或游标翻尽）：总量标量另跑一次只读 CTE 聚合
        tot = conn.execute("""
        WITH scanned AS (
          SELECT COUNT(*) AS n_hashed,
                 SUM(size_bytes >= :min) AS n_in,
                 SUM(CASE WHEN size_bytes >= :min THEN size_bytes END) AS bytes_in,
                 MIN(CASE WHEN size_bytes >= :min THEN size_bytes END) AS keep
          FROM entries
          WHERE type='file' AND hash_hex IS NOT NULL AND hash_hex != ''
          GROUP BY hash_hex)
        SELECT CAST(SUM(n_hashed) AS INTEGER),
               COUNT(*) FILTER (WHERE n_in > 1),
               CAST(COALESCE(SUM(bytes_in - keep) FILTER (WHERE n_in > 1), 0) AS INTEGER)
        FROM scanned""", {"min": min_size}).fetchone()
        hashed_files, n_groups, total_wasted = tot

    # 路径取样：TEMP 表装本页哈希，窗口函数一次顺序扫取每组前 20 条路径
    if items:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS dup_page(hash_hex TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM dup_page")
        conn.executemany("INSERT INTO dup_page(hash_hex) VALUES (?)",
                         [(it["hash_hex"],) for it in items])
        path_rows = conn.execute(
            "SELECT hash_hex, path FROM ("
            "  SELECT e.hash_hex, e.path,"
            "   ROW_NUMBER() OVER (PARTITION BY e.hash_hex ORDER BY e.path) AS rn"
            "  FROM entries e"
            "  WHERE e.hash_hex IN (SELECT hash_hex FROM dup_page) AND e.type='file')"
            " WHERE rn <= ? ORDER BY hash_hex, path",
            (PATHS_PER_GROUP + 1,)).fetchall()
        by_hash: "dict[str, list[str]]" = {}
        truncated: "dict[str, bool]" = {}
        for h, p in path_rows:
            lst = by_hash.setdefault(h, [])
            if len(lst) < PATHS_PER_GROUP:
                lst.append(p)
            else:
                truncated[h] = True
        for it in items:
            h = it["hash_hex"]
            it["paths"] = by_hash.get(h, [])
            it["paths_truncated"] = truncated.get(h, False)
    else:
        for it in items:
            it["paths"] = []
            it["paths_truncated"] = False

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_dup_cursor(last[3], last[2], last[0])

    return {
        "snapshot_id": snapshot_id,
        "hash_algo": hash_algo,
        "min_size": min_size,
        "hashed_files": hashed_files,
        "duplicate_groups": n_groups,
        "total_wasted_bytes": total_wasted,
        "items": items,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


# ---------------------------------------------------------------- CSV

CSV_HEADER = ["hash_hex", "size_bytes", "count", "wasted_bytes",
              "paths_truncated", "paths"]


def duplicates_csv(result: dict) -> str:
    """报告 dict → CSV 文本（items 全字段；paths 组内以 ' | ' 连接）。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_HEADER)
    for it in result["items"]:
        w.writerow([it["hash_hex"], it["size_bytes"], it["count"],
                    it["wasted_bytes"], it["paths_truncated"],
                    " | ".join(it["paths"])])
    return buf.getvalue()


# ---------------------------------------------------------------- HTML

def render_duplicates_html(result: dict, generated_at: str = "") -> str:
    """报告 dict → 自包含 HTML（风格对齐 report.py：内联 CSS、<details> 折叠、
    动态文本一律 html.escape）。"""
    esc = lambda v: html.escape("" if v is None else str(v), quote=True)
    r = result
    rows: list[str] = []
    for it in r["items"]:
        paths_html = "".join(f"<li><code>{esc(p)}</code></li>" for p in it["paths"])
        trunc = ('<p class="truncated">路径未全部列出</p>'
                 if it["paths_truncated"] else "")
        rows.append(
            f"<details><summary>{fmt_bytes(it['wasted_bytes'])} 浪费 · "
            f"{it['count']} 个 × {fmt_bytes(it['size_bytes'])} · "
            f"<code>{esc(it['hash_hex'][:16])}…</code></summary>"
            f"<ul>{paths_html}</ul>{trunc}</details>")
    gen = esc(generated_at)
    gen_html = f'<p class="muted">生成于 {gen}</p>' if gen else ""
    more = ('<p class="muted">仅显示前 '
            f"{len(r['items'])} 组（共 {r['duplicate_groups']} 组），"
            "完整清单请用 CLI 导出 CSV</p>"
            if len(r["items"]) < r["duplicate_groups"] else "")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>重复文件报告 · {esc(r["snapshot_id"])}</title><style>{_CSS}</style></head>
<body><main>
<h1>重复文件报告</h1>
{gen_html}
<div class="cards">
  <div class="card"><div class="num">{r["duplicate_groups"]}</div>
    <div class="lbl">重复组数</div></div>
  <div class="card"><div class="num">{fmt_bytes(r["total_wasted_bytes"])}</div>
    <div class="lbl">可回收空间</div></div>
  <div class="card"><div class="num">{r["hashed_files"]:,}</div>
    <div class="lbl">已哈希文件</div></div>
  <div class="card"><div class="num">{esc(r["hash_algo"])}</div>
    <div class="lbl">哈希算法</div></div>
  <div class="card"><div class="num">{fmt_bytes(r["min_size"])}</div>
    <div class="lbl">大小下限</div></div>
</div>
{more}
{''.join(rows) if rows else '<p class="muted">没有满足条件的重复文件。</p>'}
</main></body></html>"""
