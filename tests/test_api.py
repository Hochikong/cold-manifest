"""只读查询 API 测试：tmp 合成快照库 + catalog，TestClient 走完整 lifespan。"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import catalog_path, ensure_disk, ensure_volume, register_snapshot
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.server import create_app


# ---------------------------------------------------------------- fixtures


def _build_snapshot_db(path: Path) -> None:
    """合成一个含目录层级/rollup/skipped 的小快照库（生产布局）。

    结构（与导入引擎一致：entry_id=1 为 root 行 path='.'，顶层条目 parent_id=1）：
      1 root('.')
      ├─ 2 docs(dir)      rollup: 3 文件 / 1 目录 / 7000 B
      │   ├─ 3 a.txt      1000 B
      │   ├─ 4 b.txt      2000 B  mtime=111
      │   └─ 5 sub(dir)   rollup: 1 文件 / 4000 B
      │       └─ 6 c.log  4000 B
      └─ 7 big.bin        900000 B  mtime=222
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    init_snapshot(conn)
    conn.executemany(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type, size_bytes,"
        " mtime_ns, ext) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (1, 0, ".", "", 0, "dir", None, None, ""),
            (2, 1, "docs", "docs", 1, "dir", None, None, ""),
            (3, 2, "docs/a.txt", "a.txt", 2, "file", 1000, 100, ".txt"),
            (4, 2, "docs/b.txt", "b.txt", 2, "file", 2000, 111, ".txt"),
            (5, 2, "docs/sub", "sub", 2, "dir", None, None, ""),
            (6, 5, "docs/sub/c.log", "c.log", 3, "file", 4000, 333, ".log"),
            (7, 1, "big.bin", "big.bin", 1, "file", 900000, 222, ".bin"),
        ],
    )
    conn.executemany(
        "INSERT INTO dir_rollup(entry_id, file_count, dir_count, total_bytes,"
        " total_allocated, max_mtime_ns) VALUES(?,?,?,?,?,?)",
        [
            (2, 3, 1, 7000, 7168, 333),
            (5, 1, 0, 4000, 4096, 333),
        ],
    )
    conn.executemany(
        "INSERT INTO skipped(path, warning_type, stage, detail) VALUES(?,?,?,?)",
        [
            ("x.sys", "access_denied", "scan", "拒绝访问"),
            ("y.tmp", "reparse_point", "scan", "跳过链接"),
        ],
    )
    conn.execute("INSERT INTO meta(key, value) VALUES('source', 'test')")
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _build_snapshot_db(data_root / "vol" / "20260101T000000Z" / "snapshot.db")

    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    ensure_disk(cat, "DISK1", physical_model="TOSHIBA", capacity_bytes=2000)
    ensure_volume(cat, "vol", "DISK1", filesystem="NTFS")
    register_snapshot(cat, "vol/20260101T000000Z", "vol", status="sealed", host_path="/test",
                      file_count=4, dir_count=2, total_bytes=907000, hash_policy="none",
                      zero_byte_count=0, max_depth=3, skipped_count=2)
    cat.commit()
    cat.close()

    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c


SID = "vol/20260101T000000Z"
BASE = "/api/snapshots"


# ---------------------------------------------------------------- 基础 / 404 / 400


def test_snapshots_list(client: TestClient) -> None:
    r = client.get(BASE)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["snapshot_id"] == SID
    assert it["volume_id"] == "vol"
    assert it["file_count"] == 4
    assert it["disk_id"] == "DISK1"
    assert it["physical_model"] == "TOSHIBA"


def test_snapshot_detail(client: TestClient) -> None:
    r = client.get(f"{BASE}/{SID}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "sealed"
    assert body["meta"] == {"source": "test"}
    assert body["volume"]["filesystem"] == "NTFS"


def test_unknown_snapshot_404(client: TestClient) -> None:
    assert client.get(f"{BASE}/nope/123").status_code == 404
    assert client.get(f"{BASE}/nope/123/entries").status_code == 404
    assert client.get(f"{BASE}/nope/123/stats").status_code == 404


def test_invalid_params_400(client: TestClient) -> None:
    assert client.get(f"{BASE}/{SID}/entries", params={"limit": 9999}).status_code == 400
    assert client.get(f"{BASE}/{SID}/entries", params={"cursor": "!!!"}).status_code == 400
    assert client.get(f"{BASE}/{SID}/entries", params={"sort": "bogus"}).status_code == 400


# ---------------------------------------------------------------- entries 分页


def _page(client: TestClient, **params: object) -> dict:
    r = client.get(f"{BASE}/{SID}/entries", params=params)
    assert r.status_code == 200
    return r.json()


def test_entries_name_pagination_and_cursor(client: TestClient) -> None:
    seen: list[str] = []
    cursor = None
    while True:
        page = _page(client, parent_id=2, limit=1, cursor=cursor) if cursor \
            else _page(client, parent_id=2, limit=1)
        seen += [i["name"] for i in page["items"]]
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
        assert cursor
    assert seen == ["a.txt", "b.txt", "sub"]  # NOCASE 字典序


def test_entries_desc_order_and_rollup(client: TestClient) -> None:
    page = _page(client, parent_id=2, order="desc", limit=2)
    assert [i["name"] for i in page["items"]] == ["sub", "b.txt"]
    rollup = next(i for i in page["items"] if i["type"] == "dir")["rollup"]
    assert rollup["file_count"] == 1 and rollup["total_bytes"] == 4000


def test_entries_size_sort_includes_dirs(client: TestClient) -> None:
    page = _page(client, parent_id=2, sort="size", order="desc")
    # 目录 size 为 NULL → COALESCE 归一为 -1，排在文件之后
    names = [i["name"] for i in page["items"]]
    assert names == ["b.txt", "a.txt", "sub"]


def test_entries_filters(client: TestClient) -> None:
    assert [i["name"] for i in _page(client, parent_id=2, ext="txt")["items"]] == ["a.txt", "b.txt"]
    assert [i["name"] for i in _page(client, parent_id=2, type="dir")["items"]] == ["sub"]
    assert [i["name"] for i in _page(client, parent_id=2, min_size=1500)["items"]] == ["b.txt"]
    assert [i["name"] for i in _page(client, parent_id=2, q="b")["items"]] == ["b.txt", "sub"]
    assert _page(client, parent_id=0)["direct_child_counts"] == {"files": 1, "dirs": 1,
                                                                "symlinks": 0, "others": 0}
    # parent_id=0 解析为 root：根层 = big.bin + docs，parent_path 为 '.'
    root_page = _page(client, parent_id=0)
    assert root_page["parent_path"] == "."
    assert [i["name"] for i in root_page["items"]] == ["big.bin", "docs"]


def test_root_level_browse_no_parent_param(client: TestClient) -> None:
    """M2 回归：不带 parent_id（默认 0）时返回根层真实条目，无幻影 root 行。"""
    entries = _page(client)
    assert [i["name"] for i in entries["items"]] == ["big.bin", "docs"]
    assert entries["parent_path"] == "."
    assert all(i["type"] in ("file", "dir") for i in entries["items"])
    tree = client.get(f"{BASE}/{SID}/tree").json()
    assert tree["parent_path"] == "."
    assert [d["name"] for d in tree["dirs"]] == ["docs"]
    assert tree["direct_file_count"] == 1
    du = client.get(f"{BASE}/{SID}/du").json()
    assert [(i["name"], i["total_bytes"]) for i in du["items"]] == [
        ("big.bin", 900000), ("docs", 7000)]


def test_entries_mixed_case_pagination_no_loss(client: TestClient) -> None:
    """M3 回归：混合大小写名称 + NOCASE keyset 翻页必须全量返回、无遗漏。"""
    # 向快照库直插混合大小写条目（docs 下，entry 2 = docs）
    sid_dir = Path(client.app.state.cldm.data_root) / "vol" / "20260101T000000Z"
    conn = sqlite3.connect(sid_dir / "snapshot.db")
    conn.executemany(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type, size_bytes,"
        " mtime_ns, ext) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (10, 2, "docs/Bravo", "Bravo", 2, "file", 1, 1, ""),
            (11, 2, "docs/alpha", "alpha", 2, "file", 1, 1, ".txt"),
            (12, 2, "docs/Charlie", "Charlie", 2, "file", 1, 1, ""),
            (13, 2, "docs/delta", "delta", 2, "file", 1, 1, ".txt"),
        ],
    )
    conn.commit()
    conn.close()
    seen: list[str] = []
    cursor = None
    while True:
        params: dict = {"parent_id": 2, "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = _page(client, **params)
        seen += [i["name"] for i in page["items"]]
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    # NOCASE 序：alpha, Bravo, Charlie, delta —— limit=2 也一个不丢
    assert sorted(seen, key=str.casefold) == seen
    assert len(seen) == len(set(seen)) == 7


def test_entries_mtime_ns_as_string(client: TestClient) -> None:
    page = _page(client, parent_id=2, type="file")
    assert all(isinstance(i["mtime_ns"], str) for i in page["items"])


# ---------------------------------------------------------------- tree / du


def test_tree(client: TestClient) -> None:
    r = client.get(f"{BASE}/{SID}/tree", params={"parent_id": 0})
    body = r.json()
    assert body["direct_file_count"] == 1
    assert [d["name"] for d in body["dirs"]] == ["docs"]
    assert body["dirs"][0]["total_bytes"] == 7000 and body["dirs"][0]["file_count"] == 3


def test_du(client: TestClient) -> None:
    body = client.get(f"{BASE}/{SID}/du", params={"parent_id": 2}).json()
    assert [(i["name"], i["total_bytes"]) for i in body["items"]] == [
        ("sub", 4000), ("b.txt", 2000), ("a.txt", 1000)]


def test_du_limit(client: TestClient) -> None:
    body = client.get(f"{BASE}/{SID}/du", params={"parent_id": 2, "limit": 1}).json()
    assert len(body["items"]) == 1 and body["items"][0]["name"] == "sub"


# ---------------------------------------------------------------- stats / search / skipped


def test_stats(client: TestClient) -> None:
    body = client.get(f"{BASE}/{SID}/stats").json()
    assert body["zero_byte_count"] == 0
    assert {"ext": ".bin", "total_bytes": 900000} in body["ext_top_by_bytes"]
    assert {"ext": ".txt", "count": 2} in body["ext_top_by_count"]
    assert body["top_files"][0]["name"] == "big.bin"
    depth = {d["depth"]: d["count"] for d in body["depth_histogram"]}
    assert depth == {1: 1, 2: 2, 3: 1}
    total_hist = sum(b["count"] for b in body["size_histogram"])
    assert total_hist == 4


def test_search_prefix_pagination(client: TestClient) -> None:
    seen: list[str] = []
    cursor = None
    while True:
        params: dict = {"q": "b", "limit": 1}
        if cursor:
            params["cursor"] = cursor
        r = client.get(f"{BASE}/{SID}/search", params=params)
        assert r.status_code == 200
        page = r.json()
        seen += [i["name"] for i in page["items"]]
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert seen == ["b.txt", "big.bin"]


def test_search_filters(client: TestClient) -> None:
    r = client.get(f"{BASE}/{SID}/search", params={"q": "a", "ext": ".txt"})
    assert [i["name"] for i in r.json()["items"]] == ["a.txt"]


def test_skipped(client: TestClient) -> None:
    r = client.get(f"{BASE}/{SID}/skipped")
    assert r.status_code == 200
    body = r.json()
    assert body["has_more"] is False
    assert [i["path"] for i in body["items"]] == ["x.sys", "y.tmp"]
    r2 = client.get(f"{BASE}/{SID}/skipped", params={"warning_type": "reparse_point"})
    assert [i["path"] for i in r2.json()["items"]] == ["y.tmp"]


# ---------------------------------------------------------------- volumes / diffs


def test_volumes(client: TestClient) -> None:
    body = client.get("/api/volumes").json()
    assert body["items"][0]["volume_id"] == "vol"
    assert body["items"][0]["snapshot_count"] == 1
    assert body["items"][0]["physical_model"] == "TOSHIBA"
    assert client.get("/api/volumes/nope").status_code == 404


def test_diffs_empty(client: TestClient) -> None:
    body = client.get("/api/diffs").json()
    assert body == {"items": [], "count": 0}


def test_settings(client: TestClient) -> None:
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["version"]
    assert body["data_root"]


# ---------------------------------------------------------------- M5: LRU evict


def test_lru_evict_does_not_close_in_flight_connection(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """M5 回归：第 N+1 个快照触发 evict 时不得 close 仍在途的连接。"""
    from cold_manifest.api.state import AppState

    monkeypatch.setattr("cold_manifest.api.state._POOL_SIZE", 1)
    data_root = tmp_path / "data"
    for vid in ("v1", "v2"):
        _build_snapshot_db(data_root / vid / "20260101T000000Z" / "snapshot.db")
    state = AppState(data_root)
    try:
        c1 = state.snapshot_db("v1/20260101T000000Z")
        cur1 = c1.execute("SELECT name FROM entries ORDER BY entry_id")
        assert cur1.fetchmany(2)  # 在途游标已开启
        c2 = state.snapshot_db("v2/20260101T000000Z")   # 触发 evict v1
        assert c2.execute("SELECT COUNT(*) FROM entries").fetchone()[0] > 0
        # 被 evict 的连接仍可继续读（流式导出场景），不报 closed database
        assert cur1.fetchmany(100) is not None
        assert c1.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 7
    finally:
        state.close()   # retired 连接统一关闭（不抛错）
