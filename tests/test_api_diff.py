"""diff 端点与快照导出端点测试：TestClient 走完整 lifespan，合成两个快照库。"""

import io
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import (catalog_path, register_snapshot,
                                   snapshot_path)
from cold_manifest.db import init_catalog
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


SNAP_A_ROWS = [
    ("docs", "dir", None, 100),
    ("docs/same.txt", "file", 10, 111),
    ("docs/size.txt", "file", 10, 222),
    ("docs/mtime.txt", "file", 10, 333),
    ("docs/typeme", "file", 5, 444),
    ("gone/only_a.txt", "file", 7, 555),
]
SNAP_B_ROWS = [
    ("docs", "dir", None, 100),
    ("docs/same.txt", "file", 10, 111),
    ("docs/size.txt", "file", 99, 222),
    ("docs/mtime.txt", "file", 10, 999999),
    ("docs/typeme", "dir", None, 444),
    ("new/only_b.txt", "file", 3, 666),
]

# 与 tests/test_diff.py 同构：added=2 removed=2 size=1 mtime=1 type=1 identical=3


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _mk_snapshot(data_root, "VOL_P0", "20260101T000000Z", SNAP_A_ROWS)
    _mk_snapshot(data_root, "VOL_P0", "20260202T000000Z", SNAP_B_ROWS)

    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    for sid, vol in ((A, "VOL_P0"), (B, "VOL_P0")):
        register_snapshot(cat, sid, vol, status="sealed", host_path="/test",
                          file_count=6, dir_count=2, total_bytes=35, hash_policy="none")
    cat.commit()
    cat.close()

    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c


@pytest.fixture()
def diff_id(client: TestClient) -> str:
    r = client.post("/api/diffs", json={"a": "VOL_P0/20260101T000000Z",
                                        "b": "VOL_P0/20260202T000000Z"})
    assert r.status_code == 200
    return r.json()["diff_id"]


A = "VOL_P0/20260101T000000Z"
B = "VOL_P0/20260202T000000Z"


# ---------------------------------------------------------------- POST 物化


def test_post_diff_idempotent(client: TestClient) -> None:
    body1 = client.post("/api/diffs", json={"a": A, "b": B}).json()
    assert body1["reused"] is False
    assert body1["status"] == "done"
    assert body1["counts"]["added"] == 2
    assert body1["counts"]["identical"] == 3

    body2 = client.post("/api/diffs", json={"a": A, "b": B}).json()
    assert body2["reused"] is True
    assert body2["diff_id"] == body1["diff_id"]
    assert body2["counts"] == body1["counts"]


def test_post_diff_errors(client: TestClient) -> None:
    # 快照不存在 → 404
    r = client.post("/api/diffs", json={"a": A, "b": "NOPE/20990101T000000Z"})
    assert r.status_code == 404
    # 空 a/b → 400
    assert client.post("/api/diffs", json={"a": "", "b": B}).status_code == 400
    # M6 回归：未知选项 → 400（不得静默接受污染幂等键）
    r = client.post("/api/diffs", json={"a": A, "b": B, "options": {"depth": 0}})
    assert r.status_code == 400
    assert client.post("/api/diffs",
                       json={"a": A, "b": B, "options": {"hash": "sha256"}}).status_code == 400


# ---------------------------------------------------------------- detail / summary


def test_diff_detail(client: TestClient, diff_id: str) -> None:
    body = client.get(f"/api/diffs/{diff_id}").json()
    assert body["diff_id"] == diff_id
    assert body["status"] == "done"
    assert body["a"] == A and body["b"] == B
    assert client.get("/api/diffs/" + "0" * 16).status_code == 404


def test_diff_summary(client: TestClient, diff_id: str) -> None:
    body = client.get(f"/api/diffs/{diff_id}/summary").json()
    assert body["total_changes"] == 7  # 2+2+1+1+1，identical 不落库
    assert body["total_size_delta"] == 80  # +89(size) -4(gone/new 文件) -5(typeme file→dir)
    by_dir = {d["parent_dir"]: d for d in body["by_parent_dir"]}
    assert by_dir["docs"]["count"] == 3
    assert by_dir["gone"]["count"] == 1 and by_dir["new"]["count"] == 1


# ---------------------------------------------------------------- entries


def test_entries_filter_and_pagination(client: TestClient, diff_id: str) -> None:
    base = f"/api/diffs/{diff_id}/entries"
    # M4 回归：mtime_ns 为字符串（JS 精度）；M7 回归：a_type/b_type 列
    r = client.get(base, params={"category": "type_changed"})
    it = r.json()["items"][0]
    assert it["a_type"] == "file" and it["b_type"] == "dir"
    r = client.get(base, params={"category": "mtime_changed"})
    it = r.json()["items"][0]
    assert isinstance(it["a_mtime_ns"], str) and isinstance(it["b_mtime_ns"], str)
    # category 过滤
    r = client.get(base, params={"category": "added"})
    assert [i["path"] for i in r.json()["items"]] == ["new", "new/only_b.txt"]
    # path_prefix 过滤
    r = client.get(base, params={"path_prefix": "docs/"})
    items = r.json()["items"]
    assert all(i["path"].startswith("docs/") for i in items)
    assert len(items) == 3  # same.txt identical 不落库
    # 分页走完全集
    seen: list[str] = []
    cursor = None
    while True:
        params: dict = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get(base, params=params).json()
        seen += [i["path"] for i in page["items"]]
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
    assert len(seen) == 7 and len(set(seen)) == 7


def test_entries_errors(client: TestClient, diff_id: str) -> None:
    base = f"/api/diffs/{diff_id}/entries"
    assert client.get(base, params={"category": "bogus"}).status_code == 400
    assert client.get(base, params={"cursor": "!!!"}).status_code == 400
    assert client.get(base, params={"limit": 9999}).status_code == 400
    assert client.get("/api/diffs/" + "0" * 16 + "/entries").status_code == 404


# ---------------------------------------------------------------- diff 导出


def test_diff_export_csv(client: TestClient, diff_id: str) -> None:
    r = client.get(f"/api/diffs/{diff_id}/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.splitlines()
    assert lines[0] == "category,path,type,size_a,size_b,mtime_a,mtime_b"
    cats = {ln.split(",")[0] for ln in lines[1:]}
    assert cats == {"added", "removed", "size_changed", "mtime_changed", "type_changed"}


def test_diff_export_unknown(client: TestClient) -> None:
    assert client.get("/api/diffs/" + "0" * 16 + "/export").status_code == 404
    # format 只支持 csv，其他值 → 400
    assert client.get(f"/api/diffs/{'0' * 16}/export",
                      params={"format": "xlsx"}).status_code == 400


# ---------------------------------------------------------------- 快照导出


def test_snapshot_export_csv(client: TestClient) -> None:
    r = client.get(f"/api/snapshots/{A}/export", params={"format": "csv"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.splitlines()
    assert "entry_id" in lines[0]
    assert any("docs/same.txt" in ln for ln in lines[1:])


def test_snapshot_export_v1_csv_zip(client: TestClient) -> None:
    r = client.get(f"/api/snapshots/{B}/export", params={"format": "v1_csv"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(zf.namelist()) == ["metadata.csv", "tree.csv", "warnings.csv"]
    tree_lines = zf.read("tree.csv").decode("utf-8").splitlines()
    assert tree_lines[0].startswith("volume_id,relative_path,entry_name,entry_type")
    assert "VOL_P0,docs/same.txt,same.txt,file,10,2026-01-01T00:00:01Z" in \
        [ln for ln in tree_lines] or any("same.txt" in ln for ln in tree_lines[1:])


def test_snapshot_export_errors(client: TestClient) -> None:
    assert client.get("/api/snapshots/NOPE/1/export").status_code == 404
    assert client.get(f"/api/snapshots/{A}/export",
                      params={"format": "parquet"}).status_code == 400


# ---------------------------------------------------------------- M3: 混合大小写翻页


def test_diff_entries_mixed_case_pagination_no_loss(tmp_path: Path) -> None:
    """M3 回归：path 排序 binary ORDER BY 与 NOCASE keyset 不一致会静默丢行。"""
    data_root = tmp_path / "data"
    data_root.mkdir()
    rows = [("Bravo", "file", 1, 1), ("alpha", "file", 1, 1),
            ("Charlie", "file", 1, 1), ("delta", "file", 1, 1)]
    a = _mk_snapshot(data_root, "MC", "20260101T000000Z", rows)
    b = _mk_snapshot(data_root, "MC", "20260202T000000Z",
                     [(p, "file", 2, 1) for p, *_ in rows])
    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    for sid in (a, b):
        register_snapshot(cat, sid, "MC", status="sealed", host_path="/t",
                          file_count=4, dir_count=0, total_bytes=8, hash_policy="none")
    cat.commit()
    cat.close()

    with TestClient(create_app(data_root=str(data_root))) as c:
        did = c.post("/api/diffs", json={"a": a, "b": b}).json()["diff_id"]
        seen: list[str] = []
        cursor = None
        while True:
            params: dict = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = c.get(f"/api/diffs/{did}/entries", params=params).json()
            seen += [i["path"] for i in page["items"]]
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
    # NOCASE 序且无遗漏无重复
    assert seen == sorted(seen, key=str.casefold)
    assert len(seen) == len(set(seen)) == 4
