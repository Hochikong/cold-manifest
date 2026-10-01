"""快照内重复文件报告（P3-A + 查重三档）：content / name / fingerprint 三种模式。

三档语义（契约见 API GET /api/snapshots/{sid}/duplicates?mode=）：
- content：按完整哈希分组（现状）。只认 meta.hash_policy == 'full'（非 full →
  DuplicatesError，CLI 退出码 2 / API 400），逐条目 policy 未落库，混跑从严。
- name：按 name COLLATE NOCASE 分组（仅 file 行、size ≥ min_size）。不需要哈希；
  item 的 wasted_bytes 恒为 null（同名不同大小/不同内容无法断言浪费）；
  hashed_files = 参与匹配（≥ min_size）的文件数。
- fingerprint：按 (size_bytes, hash_hex) 分组，纳入 hash_state ∈ {full, sampled}
  的条目（NULL/error 排除）。不要求全量哈希；item 带 verified（组内全 full →
  true，含 sampled → false）。快照中完全没有可用哈希 → DuplicatesError，
  提示先跑 `cldm hash <sid> --policy sampled`（秒级指纹）或 `--policy full`。
  指纹级结论：同指纹 ≠ 同内容（sampled 指纹是启发式，见 hash.py），
  对某组严谨验证用 `cldm hash <sid> --group <hash_hex>` 做全量精验。

性能（本项目经验教训，见 diff_engine._materialize_sql）：SQLite 随机行读在
HDD/WSL-VHD 上约 43µs/条，逐行探测会退化。三档全部走顺序扫：
- 分组聚合一次物化 CTE（content/fingerprint 走 idx_entries_hash partial 索引、
  name 走 idx_entries_name NOCASE 索引，index-only）；
- 每组路径取样用 TEMP 表 + 窗口函数（ROW_NUMBER），一次顺序扫出前 20 条路径；
- keyset 分页禁用 OFFSET，排序键与游标三元组严格一致：
  content/fingerprint: (wasted_bytes DESC, size_bytes DESC, hash_hex ASC)
  name: (count DESC, name COLLATE NOCASE ASC)
  ⚠ ORDER BY 与 keyset 条件的 COLLATE 必须一致（M3 教训：不一致会静默丢行），
  name 档的排序/比较全部显式标注 COLLATE NOCASE。

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

MODES = ("content", "name", "fingerprint")

_MODE_LABEL = {
    "content": "内容级（完整哈希）",
    "name": "同名级（文件名，大小写不敏感）",
    "fingerprint": "指纹级（大小+抽样指纹，未逐字节验证）",
}


class DuplicatesError(Exception):
    """重复文件报告无法生成（策略非 full / 无哈希 / 游标非法 / mode 非法）。"""


# ---------------------------------------------------------------- 游标编码
# 组合排序键无法塞进 pagination.encode_cursor 的单键形式，这里自定义同风格的
# 不透明 base64url 编码。content/fingerprint 三元组 (wasted, size, hash_hex)；
# name 二元组 (count, name)。


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


def encode_name_cursor(count: int, name: str) -> str:
    payload = json.dumps({"c": count, "n": name},
                         ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_name_cursor(cursor: str) -> "tuple[int, str]":
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        obj = json.loads(raw)
        c, n = obj["c"], obj["n"]
        if not (isinstance(c, int) and isinstance(n, str)):
            raise ValueError("cursor 字段类型错误")
        return c, n
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError,
            KeyError, TypeError, ValueError) as e:
        raise ValueError(f"非法 cursor：{e}") from e


# ---------------------------------------------------------------- 公共校验

def _check_paging(min_size: int, limit: int) -> None:
    if min_size < 0:
        raise DuplicatesError("min_size 不能为负")
    if limit < 1:
        raise DuplicatesError("limit 不能小于 1")


def _require_full_hash(conn: sqlite3.Connection) -> str:
    """meta.hash_policy 必须为 'full'，返回 hash_algo；否则抛 DuplicatesError。"""
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    policy = meta.get("hash_policy")
    if policy != "full":
        raise DuplicatesError(
            f"该快照哈希策略不是 full（hash_policy={policy or '未设置'}），"
            "内容级重复报告只认完整哈希；请先运行：cldm hash <snapshot_id> --policy full")
    return meta.get("hash_algo") or "sha256"


def _require_any_usable_hash(conn: sqlite3.Connection) -> str:
    """指纹档：快照里至少要有一条 full/sampled 哈希，返回 hash_algo（可为默认）。"""
    row = conn.execute(
        "SELECT 1 FROM entries WHERE type='file'"
        " AND hash_hex IS NOT NULL AND hash_hex != ''"
        " AND hash_state IN ('full','sampled') LIMIT 1").fetchone()
    if row is None:
        raise DuplicatesError(
            "该快照没有任何可用的文件哈希，无法做指纹级查重；请先运行："
            "cldm hash <snapshot_id> --policy sampled（秒级指纹，推荐先跑这个）"
            "或 --policy full（完整哈希，慢）")
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    return meta.get("hash_algo") or "sha256"


def _meta_algo(conn: sqlite3.Connection) -> str:
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    return meta.get("hash_algo") or "sha256"


# ---------------------------------------------------------------- 路径取样

def _sample_paths_by_hash(conn: sqlite3.Connection, keys: "list[str]",
                          extra_where: str = "",
                          bind: "tuple | None" = None
                          ) -> "tuple[dict[str, list[str]], dict[str, bool]]":
    """TEMP 表装本页 hash_hex，窗口函数一次顺序扫取每组前 N 条路径。"""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS dup_page(hash_hex TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM dup_page")
    conn.executemany("INSERT INTO dup_page(hash_hex) VALUES (?)",
                     [(h,) for h in keys])
    path_rows = conn.execute(
        "SELECT hash_hex, path FROM ("
        "  SELECT e.hash_hex, e.path,"
        "   ROW_NUMBER() OVER (PARTITION BY e.hash_hex ORDER BY e.path) AS rn"
        "  FROM entries e"
        "  WHERE e.hash_hex IN (SELECT hash_hex FROM dup_page) AND e.type='file'"
        + extra_where + ")"
        " WHERE rn <= ? ORDER BY hash_hex, path",
        (bind or ()) + (PATHS_PER_GROUP + 1,)).fetchall()
    return _collect_paths(path_rows, key_idx=0)


def _sample_paths_by_name(conn: sqlite3.Connection, names: "list[str]",
                          min_size: int) -> "tuple[dict[str, list[str]], dict[str, bool]]":
    """同名档路径取样：按 name COLLATE NOCASE 等值 JOIN（走 idx_entries_name）。"""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS name_page(gname TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM name_page")
    conn.executemany("INSERT INTO name_page(gname) VALUES (?)",
                     [(n,) for n in names])
    path_rows = conn.execute(
        "SELECT gname, path FROM ("
        "  SELECT t.gname, e.path,"
        "   ROW_NUMBER() OVER (PARTITION BY t.gname ORDER BY e.path) AS rn"
        "  FROM name_page t"
        "  JOIN entries e ON e.name COLLATE NOCASE = t.gname COLLATE NOCASE"
        "  WHERE e.type='file' AND e.size_bytes >= ?)"
        " WHERE rn <= ? ORDER BY gname, path",
        (min_size, PATHS_PER_GROUP + 1)).fetchall()
    return _collect_paths(path_rows, key_idx=0)


def _collect_paths(path_rows, key_idx: int = 0) -> "tuple[dict[str, list[str]], dict[str, bool]]":
    by_key: "dict[str, list[str]]" = {}
    truncated: "dict[str, bool]" = {}
    for row in path_rows:
        k = row[key_idx]
        lst = by_key.setdefault(k, [])
        if len(lst) < PATHS_PER_GROUP:
            lst.append(row[1])
        else:
            truncated[k] = True
    return by_key, truncated


# ---------------------------------------------------------------- content 档

def _find_by_content(conn: sqlite3.Connection, snapshot_id: str, min_size: int,
                     limit: int, cursor: "str | None") -> dict:
    hash_algo = _require_full_hash(conn)

    cursor_cond = ""
    bind: "dict[str, Any]" = {"min": min_size, "limit": limit + 1}
    if cursor is not None:
        w, s, h = decode_dup_cursor(cursor)
        cursor_cond = (" AND (wasted < :c0 OR (wasted = :c1 AND sz < :c2)"
                       " OR (wasted = :c3 AND sz = :c4 AND hash_hex > :c5))")
        bind.update(c0=w, c1=w, c2=s, c3=w, c4=s, c5=h)

    # 单趟顺序扫：scanned 按 hash_hex 聚合，min_size 下推进聚合条件；
    # wasted = 组内字节和 − 保留一份（最小拷贝）；总量/已哈希数用标量子查询
    # 复用物化 CTE；多取 1 行判断 has_more，避免"恰好取满 → 空尾页"。
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
    else:
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

    if items:
        by_hash, truncated = _sample_paths_by_hash(conn, [it["hash_hex"] for it in items])
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
        "mode": "content",
        "hash_algo": hash_algo,
        "min_size": min_size,
        "hashed_files": hashed_files,
        "duplicate_groups": n_groups,
        "total_wasted_bytes": total_wasted,
        "items": items,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


# ---------------------------------------------------------------- fingerprint 档

_FP_WHERE = ("type='file' AND size_bytes >= :min"
             " AND hash_hex IS NOT NULL AND hash_hex != ''"
             " AND hash_state IN ('full','sampled')")


def _find_by_fingerprint(conn: sqlite3.Connection, snapshot_id: str, min_size: int,
                         limit: int, cursor: "str | None") -> dict:
    hash_algo = _require_any_usable_hash(conn)

    # 排序键 (wasted DESC, sz DESC, hash ASC) 与 content 档一致；sz 是分组键的
    # 一半，同 hash 必同 sz，键无歧义。游标复用三元组编码。
    cursor_cond = ""
    bind: "dict[str, Any]" = {"min": min_size, "limit": limit + 1}
    if cursor is not None:
        w, s, h = decode_dup_cursor(cursor)
        cursor_cond = (" AND (wasted < :c0 OR (wasted = :c1 AND sz < :c2)"
                       " OR (wasted = :c3 AND sz = :c4 AND hash_hex > :c5))")
        bind.update(c0=w, c1=w, c2=s, c3=w, c4=s, c5=h)

    sql = """
    WITH scanned AS MATERIALIZED (
      SELECT hash_hex, size_bytes AS sz,
             COUNT(*) AS n,
             SUM(size_bytes) AS bytes_in,
             SUM(hash_state = 'full') AS n_full
      FROM entries
      WHERE {where}
      GROUP BY hash_hex, size_bytes)
    SELECT hash_hex, n AS cnt, sz, bytes_in - sz AS wasted,
           (n_full = n),
           (SELECT CAST(SUM(n) AS INTEGER) FROM scanned),
           (SELECT COUNT(*) FROM scanned WHERE n > 1),
           (SELECT CAST(COALESCE(SUM(bytes_in - sz), 0) AS INTEGER)
            FROM scanned WHERE n > 1)
    FROM scanned
    WHERE n > 1{cursor}
    ORDER BY wasted DESC, sz DESC, hash_hex ASC
    LIMIT :limit""".format(where=_FP_WHERE, cursor=cursor_cond)
    rows = conn.execute(sql, bind).fetchall()
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    items = [{"hash_hex": r[0], "size_bytes": r[2], "count": r[1],
              "wasted_bytes": r[3], "verified": bool(r[4])} for r in rows]
    if rows:
        hashed_files, n_groups, total_wasted = rows[0][5], rows[0][6], rows[0][7]
    else:
        tot = conn.execute("""
        SELECT CAST(COALESCE(SUM(n), 0) AS INTEGER),
               COUNT(*) FILTER (WHERE n > 1),
               CAST(COALESCE(SUM(bytes_in - sz) FILTER (WHERE n > 1), 0) AS INTEGER)
        FROM (SELECT hash_hex, size_bytes AS sz, COUNT(*) AS n, SUM(size_bytes) AS bytes_in
              FROM entries WHERE {where}
              GROUP BY hash_hex, size_bytes)""".format(where=_FP_WHERE), {"min": min_size}).fetchone()
        hashed_files, n_groups, total_wasted = tot

    if items:
        by_hash, truncated = _sample_paths_by_hash(conn, [it["hash_hex"] for it in items],
                                                   extra_where=" AND e.hash_state IN ('full','sampled')")
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
        "mode": "fingerprint",
        "hash_algo": hash_algo,
        "min_size": min_size,
        "hashed_files": hashed_files,
        "duplicate_groups": n_groups,
        "total_wasted_bytes": total_wasted,
        "items": items,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


# ---------------------------------------------------------------- name 档

def _find_by_name(conn: sqlite3.Connection, snapshot_id: str, min_size: int,
                  limit: int, cursor: "str | None") -> dict:
    # 排序键 (cnt DESC, name COLLATE NOCASE ASC)；keyset 条件同样显式 NOCASE
    #（M3 教训：ORDER BY 与 keyset 的 COLLATE 不一致会静默丢行）。
    cursor_cond = ""
    bind: "dict[str, Any]" = {"min": min_size, "limit": limit + 1}
    if cursor is not None:
        c, n = decode_name_cursor(cursor)
        cursor_cond = (" AND (cnt < :c0"
                       " OR (cnt = :c1 AND gname COLLATE NOCASE > :cn COLLATE NOCASE))")
        bind.update(c0=c, c1=c, cn=n)

    sql = """
    WITH scanned AS MATERIALIZED (
      SELECT MIN(name) AS gname,
             COUNT(*) AS cnt,
             SUM(size_bytes) AS bytes_in
      FROM entries
      WHERE type='file' AND size_bytes >= :min
      GROUP BY name COLLATE NOCASE)
    SELECT gname, cnt, bytes_in,
           (SELECT CAST(SUM(cnt) AS INTEGER) FROM scanned),
           (SELECT COUNT(*) FROM scanned WHERE cnt > 1)
    FROM scanned
    WHERE cnt > 1{cursor}
    ORDER BY cnt DESC, gname COLLATE NOCASE ASC
    LIMIT :limit""".format(cursor=cursor_cond)
    rows = conn.execute(sql, bind).fetchall()
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    items = [{"name": r[0], "count": r[1], "size_bytes": r[2],
              "wasted_bytes": None} for r in rows]
    if rows:
        hashed_files, n_groups = rows[0][3], rows[0][4]
    else:
        tot = conn.execute("""
        SELECT CAST(COALESCE(SUM(cnt), 0) AS INTEGER),
               COUNT(*) FILTER (WHERE cnt > 1)
        FROM (SELECT COUNT(*) AS cnt FROM entries
              WHERE type='file' AND size_bytes >= :min
              GROUP BY name COLLATE NOCASE)""", {"min": min_size}).fetchone()
        hashed_files, n_groups = tot

    if items:
        by_name, truncated = _sample_paths_by_name(conn, [it["name"] for it in items],
                                                   min_size)
        for it in items:
            n = it["name"]
            it["paths"] = by_name.get(n, [])
            it["paths_truncated"] = truncated.get(n, False)
    else:
        for it in items:
            it["paths"] = []
            it["paths_truncated"] = False

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_name_cursor(last[1], last[0])

    return {
        "snapshot_id": snapshot_id,
        "mode": "name",
        "hash_algo": None,
        "min_size": min_size,
        "hashed_files": hashed_files,
        "duplicate_groups": n_groups,
        "total_wasted_bytes": None,
        "items": items,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


# ---------------------------------------------------------------- 入口

def find_duplicates(
    conn: sqlite3.Connection,
    snapshot_id: str,
    *,
    mode: str = "content",
    min_size: int = DEFAULT_MIN_SIZE,
    limit: int = DEFAULT_LIMIT,
    cursor: "str | None" = None,
) -> dict:
    """快照内重复文件三档查重，返回报告 dict（API/CLI 共用形状）。

    mode：content（默认，完整哈希）/ name（同名）/ fingerprint（大小+指纹）。
    conn 为只读 immutable 连接亦可（TEMP 表写在独立 temp 库，不受影响）。
    """
    if mode not in MODES:
        raise DuplicatesError(
            f"非法 mode：{mode!r}（允许 {'/'.join(MODES)}）")
    _check_paging(min_size, limit)
    if mode == "name":
        return _find_by_name(conn, snapshot_id, min_size, limit, cursor)
    if mode == "fingerprint":
        return _find_by_fingerprint(conn, snapshot_id, min_size, limit, cursor)
    return _find_by_content(conn, snapshot_id, min_size, limit, cursor)


# ---------------------------------------------------------------- CSV

def duplicates_csv(result: dict) -> str:
    """报告 dict → CSV 文本（items 全字段；paths 组内以 ' | ' 连接）。

    首列固定为 mode（导出标注查重档位）；各档列形状不同。
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    mode = result.get("mode", "content")
    if mode == "name":
        w.writerow(["mode", "name", "count", "size_bytes", "wasted_bytes",
                    "paths_truncated", "paths"])
        for it in result["items"]:
            w.writerow(["name", it["name"], it["count"], it["size_bytes"],
                        it["wasted_bytes"], it["paths_truncated"],
                        " | ".join(it["paths"])])
    elif mode == "fingerprint":
        w.writerow(["mode", "hash_hex", "size_bytes", "count", "wasted_bytes",
                    "verified", "paths_truncated", "paths"])
        for it in result["items"]:
            w.writerow(["fingerprint", it["hash_hex"], it["size_bytes"],
                        it["count"], it["wasted_bytes"], it["verified"],
                        it["paths_truncated"], " | ".join(it["paths"])])
    else:
        w.writerow(["mode", "hash_hex", "size_bytes", "count", "wasted_bytes",
                    "paths_truncated", "paths"])
        for it in result["items"]:
            w.writerow(["content", it["hash_hex"], it["size_bytes"],
                        it["count"], it["wasted_bytes"],
                        it["paths_truncated"], " | ".join(it["paths"])])
    return buf.getvalue()


# ---------------------------------------------------------------- HTML

def render_duplicates_html(result: dict, generated_at: str = "") -> str:
    """报告 dict → 自包含 HTML（风格对齐 report.py：内联 CSS、<details> 折叠、
    动态文本一律 html.escape）。标题与脚注标注 mode；fingerprint 档注明
    "指纹级，未逐字节验证"。"""
    esc = lambda v: html.escape("" if v is None else str(v), quote=True)
    r = result
    mode = r.get("mode", "content")
    label = _MODE_LABEL.get(mode, mode)
    rows: list[str] = []
    for it in r["items"]:
        paths_html = "".join(f"<li><code>{esc(p)}</code></li>" for p in it["paths"])
        trunc = ('<p class="truncated">路径未全部列出</p>'
                 if it["paths_truncated"] else "")
        if mode == "name":
            rows.append(
                f"<details><summary>{it['count']} 个同名 · "
                f"共 {fmt_bytes(it['size_bytes'])} · "
                f"<code>{esc(it['name'])}</code></summary>"
                f"<ul>{paths_html}</ul>{trunc}</details>")
        else:
            if mode == "fingerprint":
                ver = ('<span class="muted">已验证（全 full）</span>'
                       if it["verified"] else '<span class="muted">未验证（含 sampled 指纹）</span>')
                head = (f"{fmt_bytes(it['wasted_bytes'])} 浪费 · "
                        f"{it['count']} 个 × {fmt_bytes(it['size_bytes'])} · "
                        f"<code>{esc(it['hash_hex'][:16])}…</code> · {ver}")
            else:
                head = (f"{fmt_bytes(it['wasted_bytes'])} 浪费 · "
                        f"{it['count']} 个 × {fmt_bytes(it['size_bytes'])} · "
                        f"<code>{esc(it['hash_hex'][:16])}…</code>")
            rows.append(
                f"<details><summary>{head}</summary>"
                f"<ul>{paths_html}</ul>{trunc}</details>")
    gen = esc(generated_at)
    gen_html = f'<p class="muted">生成于 {gen}</p>' if gen else ""
    more = ('<p class="muted">仅显示前 '
            f"{len(r['items'])} 组（共 {r['duplicate_groups']} 组），"
            "完整清单请用 CLI 导出 CSV</p>"
            if len(r["items"]) < r["duplicate_groups"] else "")
    if mode == "fingerprint":
        note = ('<p class="muted">⚠ 指纹级比对：按抽样指纹分组，未逐字节验证；'
                "对某一组做严谨验证请运行 "
                f"<code>cldm hash {esc(r['snapshot_id'])} --group &lt;hash_hex&gt;</code>"
                "（全量精验该组）。</p>")
    elif mode == "name":
        note = ('<p class="muted">同名级比对：仅按文件名分组（大小写不敏感），'
                "同名不代表内容相同；无需哈希。</p>")
    else:
        note = ""
    hashed_lbl = ("已哈希文件" if mode == "content"
                  else "参与匹配文件")
    wasted_card = (f'<div class="card"><div class="num">'
                   f"{fmt_bytes(r['total_wasted_bytes'])}</div>"
                   '<div class="lbl">可回收空间</div></div>'
                   if r.get("total_wasted_bytes") is not None else "")
    algo_card = (f'<div class="card"><div class="num">{esc(r["hash_algo"])}</div>'
                 '<div class="lbl">哈希算法</div></div>'
                 if r.get("hash_algo") else "")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>重复文件报告（{esc(label)}） · {esc(r["snapshot_id"])}</title><style>{_CSS}</style></head>
<body><main>
<h1>重复文件报告</h1>
<p class="muted">模式：{esc(label)}</p>
{gen_html}
{note}
<div class="cards">
  <div class="card"><div class="num">{r["duplicate_groups"]}</div>
    <div class="lbl">重复组数</div></div>
  {wasted_card}
  <div class="card"><div class="num">{r["hashed_files"]:,}</div>
    <div class="lbl">{hashed_lbl}</div></div>
  {algo_card}
  <div class="card"><div class="num">{fmt_bytes(r["min_size"])}</div>
    <div class="lbl">大小下限</div></div>
</div>
{more}
{''.join(rows) if rows else '<p class="muted">没有满足条件的重复文件。</p>'}
</main></body></html>"""
