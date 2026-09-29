"""FTS5 全文索引：封库构建、fulltext 搜索端点、build-fts CLI 补建。"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import catalog_path, ensure_disk, ensure_volume, register_snapshot
from cold_manifest.cli import main as cli_main
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.seal import build_fts, seal_snapshot
from cold_manifest.server import create_app

SID = "vol/20260101T000000Z"

_ROWS = [
    (1, 0, ".", "", 0, "dir", None, None, ""),
    (2, 1, "docs", "docs", 1, "dir", None, None, ""),
    (3, 2, "docs/report.txt", "report.txt", 2, "file", 1000, 100, ".txt"),
    (4, 2, "docs/会议纪要-2026.txt", "会议纪要-2026.txt", 2, "file", 2000, 111, ".txt"),
    (5, 2, "docs/照片备份", "照片备份", 2, "dir", None, None, ""),
    (6, 5, "docs/照片备份/春节合影.jpg", "春节合影.jpg", 3, "file", 4000, 333, ".jpg"),
    (7, 1, "big.bin", "big.bin", 1, "file", 900000, 222, ".bin"),
]


def _build_snapshot_db(path: Path, sealed: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    init_snapshot(conn)
    conn.executemany(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type, size_bytes,"
        " mtime_ns, ext) VALUES(?,?,?,?,?,?,?,?,?)",
        _ROWS,
    )
    if sealed:
        seal_snapshot(conn)
    conn.commit()
    conn.close()


@pytest.fixture()
def client_sealed(tmp_path: Path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    _build_snapshot_db(data_root / "vol" / "20260101T000000Z" / "snapshot.db", sealed=True)
    with _make_client(data_root) as c:
        yield c


@pytest.fixture()
def client_unsealed(tmp_path: Path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    _build_snapshot_db(data_root / "vol" / "20260101T000000Z" / "snapshot.db", sealed=False)
    with _make_client(data_root) as c:
        yield c


def _make_client(data_root: Path) -> TestClient:
    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    ensure_disk(cat, "DISK1", physical_model="T", capacity_bytes=2000)
    ensure_volume(cat, "vol", "DISK1", filesystem="NTFS")
    register_snapshot(cat, SID, "vol", status="sealed", host_path="/test",
                      file_count=4, dir_count=2, total_bytes=907000, hash_policy="none",
                      zero_byte_count=0, max_depth=3, skipped_count=0)
    cat.commit()
    cat.close()
    return TestClient(create_app(data_root=str(data_root)))


# -------------------------------------------------------------- 封库构建


def test_seal_creates_fts_count_matches(tmp_path: Path) -> None:
    db = tmp_path / "snapshot.db"
    _build_snapshot_db(db, sealed=True)
    conn = sqlite3.connect(db)
    n_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    n_fts = conn.execute("SELECT COUNT(*) FROM entries_fts").fetchone()[0]
    assert n_fts == n_entries
    # rowid 对齐 entries.entry_id
    assert conn.execute(
        "SELECT COUNT(*) FROM entries e WHERE NOT EXISTS"
        " (SELECT 1 FROM entries_fts f WHERE f.rowid = e.entry_id)"
    ).fetchone()[0] == 0
    conn.close()


# -------------------------------------------------------------- 搜索端点


def test_fulltext_ascii_and_chinese(client_sealed: TestClient) -> None:
    r = client_sealed.get(f"/api/snapshots/{SID}/search",
                          params={"q": "report", "mode": "fulltext"})
    assert r.status_code == 200
    body = r.json()
    assert body["fulltext_available"] is True
    assert body["mode"] == "fulltext"
    assert [i["name"] for i in body["items"]] == ["report.txt"]

    # 中文子串（trigram）：只给路径中段子串也能命中
    r = client_sealed.get(f"/api/snapshots/{SID}/search",
                          params={"q": "会议纪要", "mode": "fulltext"})
    assert [i["name"] for i in r.json()["items"]] == ["会议纪要-2026.txt"]

    r = client_sealed.get(f"/api/snapshots/{SID}/search",
                          params={"q": "春节合影", "mode": "fulltext"})
    assert [i["path"] for i in r.json()["items"]] == ["docs/照片备份/春节合影.jpg"]


def test_fulltext_fallback_without_fts(client_unsealed: TestClient) -> None:
    r = client_unsealed.get(f"/api/snapshots/{SID}/search",
                            params={"q": "report", "mode": "fulltext"})
    assert r.status_code == 200
    body = r.json()
    assert body["fulltext_available"] is False
    assert body["mode"] == "substring"
    # substring：文件名包含即命中（prefix 模式下 "eport" 不会命中）
    assert [i["name"] for i in body["items"]] == ["report.txt"]


def test_prefix_mode_reports_fulltext_available(client_sealed: TestClient) -> None:
    r = client_sealed.get(f"/api/snapshots/{SID}/search", params={"q": "rep"})
    assert r.status_code == 200
    assert r.json()["fulltext_available"] is True
    assert r.json()["mode"] == "prefix"


def test_fulltext_injection_is_escaped(client_sealed: TestClient) -> None:
    """注入类输入被当作字面短语安全处理：不 500、不越权命中。"""
    for q in ['a" OR name MATCH "txt', "*(?NEAR(", "\\", "''"]:
        r = client_sealed.get(f"/api/snapshots/{SID}/search",
                              params={"q": q, "mode": "fulltext"})
        assert r.status_code == 200, q
        assert r.json()["items"] == [], q


def test_fulltext_match_syntax_error_400(client_sealed: TestClient, monkeypatch) -> None:
    """FTS MATCH 语法错误 → 400（转义后正常输入到不了这条路径，注入见上例）。"""
    import cold_manifest.api.routes_snapshots as rs

    monkeypatch.setattr(rs, "fts_match_query", lambda q: 'NOT VALID((')
    r = client_sealed.get(f"/api/snapshots/{SID}/search",
                          params={"q": "anything", "mode": "fulltext"})
    assert r.status_code == 400


def test_fulltext_pagination(client_sealed: TestClient) -> None:
    # q="docs" 命中 path 或 name 含 docs 的行（dir docs、两个 docs/ 文件、
    # 照片备份目录的 path 前缀、春节合影的 path）——先取全量确认基准
    r = client_sealed.get(f"/api/snapshots/{SID}/search",
                          params={"q": "docs", "mode": "fulltext", "limit": 500})
    assert r.status_code == 200
    expected = [i["path"] for i in r.json()["items"]]
    assert len(expected) >= 3

    # limit=2 逐页翻取，无重复、有序、收齐
    seen: "list[str]" = []
    cursor = None
    pages = 0
    while True:
        params: dict = {"q": "docs", "mode": "fulltext", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = client_sealed.get(f"/api/snapshots/{SID}/search", params=params)
        assert r.status_code == 200
        body = r.json()
        seen += [i["path"] for i in body["items"]]
        pages += 1
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
        assert pages < 10
    assert seen == expected
    assert len(seen) == len(set(seen))
    assert seen == sorted(seen, key=str.lower)


def test_build_fts_cli_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "snapshot.db"
    _build_snapshot_db(db, sealed=False)  # 未封库也能补建（仅警告）
    argv = ["build-fts", str(db)]
    assert cli_main(argv) == 0
    conn = sqlite3.connect(db)
    n1 = conn.execute("SELECT COUNT(*) FROM entries_fts").fetchone()[0]
    conn.close()
    # 第二次执行：重建而非报错，计数一致
    assert cli_main(argv) == 0
    conn = sqlite3.connect(db)
    n2 = conn.execute("SELECT COUNT(*) FROM entries_fts").fetchone()[0]
    assert n1 == n2 == len(_ROWS)
    # 重建后索引与 entries 仍对齐
    assert build_fts(conn)[0] == len(_ROWS)
    conn.close()
