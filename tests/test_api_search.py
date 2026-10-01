"""跨快照全局搜索 API（P4.5-fix-60）：GET /api/search。"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import (catalog_path, ensure_disk, ensure_volume,
                                   register_snapshot)
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.seal import build_fts
from cold_manifest.server import create_app


def _make_db(path: Path, entries: "list[tuple]", fts: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    init_snapshot(conn)
    rows = [(1, 0, ".", "", 0, "dir", None, None)]
    rows += [(eid, 1, name, name, 1, "file", 100 + eid, 1000 + eid)
             for eid, name in entries]
    conn.executemany(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type,"
        " size_bytes, mtime_ns) VALUES(?,?,?,?,?,?,?,?)", rows)
    if fts:
        build_fts(conn)
    conn.commit()
    conn.close()


def _register(data_root: Path, volume_id: str, ts: str, collected_at: str) -> None:
    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    ensure_disk(cat, f"D_{volume_id}")
    ensure_volume(cat, volume_id, f"D_{volume_id}")
    register_snapshot(cat, f"{volume_id}/{ts}", volume_id, batch_id=1,
                      collected_at=collected_at, status="sealed", host_path="")
    cat.commit()
    cat.close()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    data_root = tmp_path / "data"
    data_root.mkdir()
    # 旧快照（带 FTS）：report.txt 等
    _make_db(data_root / "volA" / "20260101T000000Z" / "snapshot.db",
             [(2, "report.txt"), (3, "summary.txt"), (4, "notes.txt"),
              (5, "ab_old.txt")],
             fts=True)
    # 新快照（无 FTS）：同名 a*.txt 一串，供翻页测试
    _make_db(data_root / "volB" / "20260201T000000Z" / "snapshot.db",
             [(2, "ab1.txt"), (3, "ab2.txt"), (4, "ab3.txt"),
              (5, "ab4.txt"), (6, "ab5.txt")])
    _register(data_root, "volA", "20260101T000000Z", "2026-01-01T00:00:00Z")
    _register(data_root, "volB", "20260201T000000Z", "2026-02-01T00:00:00Z")
    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c


def _all_pages(client: TestClient, q: str, limit: int = 2, **params) -> "list[dict]":
    items, seen_cursors = [], set()
    r = client.get("/api/search", params={"q": q, "limit": limit, **params})
    assert r.status_code == 200, r.text
    while True:
        body = r.json()
        items.extend(body["items"])
        if not body["has_more"]:
            assert body["next_cursor"] is None
            break
        cur = body["next_cursor"]
        assert cur not in seen_cursors
        seen_cursors.add(cur)
        r = client.get("/api/search", params={"q": q, "limit": limit,
                                              "cursor": cur, **params})
        assert r.status_code == 200, r.text
    return items


# ---------------------------------------------------------------- 用例


def test_prefix_hit(client: TestClient) -> None:
    r = client.get("/api/search", params={"q": "rep"})
    assert r.status_code == 200
    body = r.json()
    assert [it["path"] for it in body["items"]] == ["report.txt"]
    it = body["items"][0]
    assert it["snapshot_id"] == "volA/20260101T000000Z"
    assert it["volume_id"] == "volA" and it["type"] == "file"
    assert body["scanned"]["snapshots"] == 2
    assert body["scanned"]["fulltext_used"] is False


def test_fulltext_hit(client: TestClient) -> None:
    r = client.get("/api/search", params={"q": "report", "mode": "fulltext"})
    assert r.status_code == 200
    body = r.json()
    paths = [it["path"] for it in body["items"]]
    assert "report.txt" in paths
    assert body["scanned"]["fulltext_used"] is True


def test_fulltext_falls_back_to_prefix(client: TestClient) -> None:
    """volB 无 FTS：fulltext 模式回退前缀仍能命中该库条目。"""
    r = client.get("/api/search", params={"q": "ab3", "mode": "fulltext"})
    assert r.status_code == 200
    body = r.json()
    assert [it["path"] for it in body["items"]] == ["ab3.txt"]
    assert body["items"][0]["snapshot_id"] == "volB/20260201T000000Z"


def test_multi_snapshot_order_stable(client: TestClient) -> None:
    """collected_at 新的快照排前；同快照内按 path 升序。"""
    items = _all_pages(client, "ab")
    paths = [it["path"] for it in items]
    assert paths == ["ab1.txt", "ab2.txt", "ab3.txt", "ab4.txt", "ab5.txt",
                     "ab_old.txt"]
    assert items[0]["snapshot_id"] == "volB/20260201T000000Z"
    assert items[-1]["snapshot_id"] == "volA/20260101T000000Z"


def test_cursor_pagination_no_gap_no_dup(client: TestClient) -> None:
    items = _all_pages(client, "ab", limit=2)
    assert len(items) == 6
    paths = [it["path"] for it in items]
    assert len(set(paths)) == 6  # 不重
    assert paths == sorted(paths)  # 不漏（全序列即排序序列）


def test_q_too_short_400(client: TestClient) -> None:
    assert client.get("/api/search", params={"q": "a"}).status_code == 400
    assert client.get("/api/search", params={"q": ""}).status_code == 400


def test_volume_filter(client: TestClient) -> None:
    r = client.get("/api/search", params={"q": "ab", "volume_id": "volA"})
    assert r.status_code == 200
    assert [it["path"] for it in r.json()["items"]] == ["ab_old.txt"]
    assert r.json()["scanned"]["snapshots"] == 1
    r = client.get("/api/search", params={"q": "ab", "volume_id": "volB"})
    assert len(r.json()["items"]) == 5


def test_type_filter_and_bad_mode(client: TestClient) -> None:
    r = client.get("/api/search", params={"q": "ab1", "type": "dir"})
    assert r.status_code == 200 and r.json()["items"] == []
    assert client.get("/api/search",
                      params={"q": "ab1", "mode": "regex"}).status_code == 400
    assert client.get("/api/search",
                      params={"q": "ab1", "type": "link"}).status_code == 400
    assert client.get("/api/search",
                      params={"q": "ab1", "cursor": "!!!"}).status_code == 400
