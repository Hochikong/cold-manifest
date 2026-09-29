"""diff HTML 报告测试：generate_diff_report + GET /api/diffs/{id}/report。

夹具与 test_api_diff.py 同构：added=2 removed=2 size=1 mtime=1 type=1 identical=3。
"""

import io
import sqlite3
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import catalog_path, register_snapshot, snapshot_path
from cold_manifest.db import init_catalog
from cold_manifest.report import generate_diff_report
from cold_manifest.schema import SNAPSHOT_DDL
from cold_manifest.server import create_app


def _mk_snapshot(data_root: Path, volume_id: str, ts: str,
                 rows: "list[tuple]") -> str:
    """建一个封库快照库，返回 snapshot_id。rows: (path, type, size, mtime)。"""
    sid = f"{volume_id}/{ts}"
    db = snapshot_path(data_root, sid)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(SNAPSHOT_DDL)
        conn.execute("INSERT INTO meta(key, value) VALUES('status', 'sealed')")
        conn.execute("INSERT INTO meta(key, value) VALUES('volume_id', ?)", (volume_id,))
        data = [(1, 0, ".", "", 0, "dir", None, None, None)]
        paths_seen = {"."}
        eid = 2
        for path, etype, size, mtime in rows:
            parts = path.split("/")
            for i in range(1, len(parts)):
                parent = "/".join(parts[:i])
                if parent not in paths_seen:
                    data.append((eid, 1, parent, parts[i - 1], i, "dir", None, None, None))
                    paths_seen.add(parent)
                    eid += 1
            depth = path.count("/") + 1
            data.append((eid, 1, path, parts[-1], depth, etype, size, size, mtime))
            paths_seen.add(path)
            eid += 1
        conn.executemany(
            "INSERT INTO entries(entry_id,parent_id,path,name,depth,type,size_bytes,"
            "allocated_bytes,mtime_ns) VALUES(?,?,?,?,?,?,?,?,?)", data)
        conn.commit()
    finally:
        conn.close()
    return sid


A = "VOL_P0/20260101T000000Z"
B = "VOL_P0/20260202T000000Z"

SNAP_A_ROWS = [
    ("docs", "dir", None, 100),
    ("docs/same.txt", "file", 10, 111),
    ("docs/size.txt", "file", 10, 222),
    ("docs/mtime.txt", "file", 10, 333),
    ("docs/typeme", "file", 5, 444),
    # 转义用例：<script>、&、引号、中文
    ('gone/<script>alert(1)&预算.txt', "file", 7, 555),
    ("gone/only_a.txt", "file", 7, 555),
]
SNAP_B_ROWS = [
    ("docs", "dir", None, 100),
    ("docs/same.txt", "file", 10, 111),
    ("docs/size.txt", "file", 99, 222),
    ("docs/mtime.txt", "file", 10, 999999),
    ("docs/typeme", "dir", None, 444),
    ('new/<script>alert(1)&预算.txt', "file", 3, 666),
    ("new/only_b.txt", "file", 3, 666),
]


@pytest.fixture()
def diff_db(tmp_path: Path) -> Path:
    """物化一个小 diff，返回结果库路径。"""
    from cold_manifest.diff_engine import diff_db_path, materialize_diff

    data_root = tmp_path / "data"
    data_root.mkdir()
    _mk_snapshot(data_root, "VOL_P0", "20260101T000000Z", SNAP_A_ROWS)
    _mk_snapshot(data_root, "VOL_P0", "20260202T000000Z", SNAP_B_ROWS)
    result = materialize_diff(data_root, A, B)
    return diff_db_path(data_root, result.diff_id)


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _mk_snapshot(data_root, "VOL_P0", "20260101T000000Z", SNAP_A_ROWS)
    _mk_snapshot(data_root, "VOL_P0", "20260202T000000Z", SNAP_B_ROWS)

    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    for sid in (A, B):
        register_snapshot(cat, sid, "VOL_P0", status="sealed", host_path="/test",
                          file_count=7, dir_count=2, total_bytes=42,
                          collected_at="2026-01-01T00:00:00Z")
    cat.commit()
    cat.close()

    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c


@pytest.fixture()
def diff_id(client: TestClient) -> str:
    r = client.post("/api/diffs", json={"a": A, "b": B})
    assert r.status_code == 200
    return r.json()["diff_id"]


class _Check(HTMLParser):
    """宽松合法性检查：解析不抛错、无 script 元素、统计 details 数。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts = 0
        self.details = 0
        self.text: "list[str]" = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.scripts += 1
        if tag == "details":
            self.details += 1

    def handle_data(self, data):
        self.text.append(data)


def _sql_counts(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        return dict(conn.execute(
            "SELECT change_type, COUNT(*) FROM diff_entries GROUP BY change_type"))
    finally:
        conn.close()


# ---------------------------------------------------------------- 生成器


def test_report_counts_match_db(diff_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "r.html"
    stats = generate_diff_report(diff_db, out)
    assert out.is_file()
    # 分类计数与库内 SQL 一致
    sql = _sql_counts(diff_db)
    for cat, n in sql.items():
        if cat == "identical":
            assert stats["counts"]["identical"] == 3  # 不落库，用物化时计数
        else:
            assert stats["counts"][cat] == n
    assert stats["total_changes"] == 9  # added3+removed3+size1+mtime1+type1
    assert stats["total_size_delta"] == 76  # +89 -5 -14 +6（与库内 SUM 同口径）
    text = out.read_text(encoding="utf-8")
    assert "净大小变化" in text
    # 目录 Top 正负着色类
    assert "pos" in text


def test_report_to_stream_and_identical_optional(diff_db: Path) -> None:
    buf = io.StringIO()
    stats = generate_diff_report(diff_db, buf)
    assert buf.getvalue().startswith("<!DOCTYPE html>")
    assert stats["counts"]["identical"] == 3


def test_report_escapes_html(diff_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "r.html"
    generate_diff_report(diff_db, out)
    text = out.read_text(encoding="utf-8")
    p = _Check()
    p.feed(text)
    p.close()
    assert p.scripts == 0          # <script> 被转义，不产生 script 元素
    assert "&lt;script&gt;" in text
    assert 'alert(1)&amp;预算.txt' in text
    assert "预算" in text and "丢失" in text  # 中文正常


def test_report_truncation_marker(diff_db: Path, tmp_path: Path) -> None:
    buf = io.StringIO()
    generate_diff_report(diff_db, buf, max_rows_per_section=1)
    text = buf.getvalue()
    assert "已截断" in text
    assert "共 3 条，已截断" in text


def test_report_missing_db(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        generate_diff_report(tmp_path / "nope.db", io.StringIO())


# ---------------------------------------------------------------- 端点


def test_report_endpoint(client: TestClient, diff_id: str) -> None:
    r = client.get(f"/api/diffs/{diff_id}/report")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["content-disposition"] == f'attachment; filename="diff-{diff_id}.html"'
    assert r.text.startswith("<!DOCTYPE html>")
    assert "&lt;script&gt;" in r.text
    p = _Check()
    p.feed(r.text)
    assert p.scripts == 0
    assert p.details >= 5  # 每个非空分类一个折叠节
    # 头部含 catalog 里的采集时间
    assert "2026-01-01T00:00:00Z" in r.text


def test_report_endpoint_errors(client: TestClient, diff_id: str, tmp_path: Path) -> None:
    # format != html → 400
    assert client.get(f"/api/diffs/{diff_id}/report",
                      params={"format": "pdf"}).status_code == 400
    # 未知 diff → 404
    assert client.get("/api/diffs/" + "0" * 16 + "/report").status_code == 404
    # 结果库被删（未物化）→ 404
    db = tmp_path / "data" / "_diffs" / f"{diff_id}.db"
    db.unlink()
    r = client.get(f"/api/diffs/{diff_id}/report")
    assert r.status_code == 404
