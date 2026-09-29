"""stats 预计算（P2-A）：封库落表、端点直读/回退、build-stats CLI 补建。"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import catalog_path, ensure_disk, ensure_volume, register_snapshot
from cold_manifest.cli import main as cli_main
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.seal import seal_snapshot
from cold_manifest.stats_cache import (
    STATS_KEYS,
    build_stats_cache,
    compute_stats,
    load_precomputed_stats,
)
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
    (8, 1, "empty.dat", "empty.dat", 1, "file", 0, 50, ".dat"),
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
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('status', 'sealed')")
        seal_snapshot(conn)
    conn.commit()
    conn.close()


def _make_client(data_root: Path) -> TestClient:
    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    ensure_disk(cat, "DISK1", physical_model="T", capacity_bytes=2000)
    ensure_volume(cat, "vol", "DISK1", filesystem="NTFS")
    register_snapshot(cat, SID, "vol", status="sealed", host_path="/test",
                      file_count=5, dir_count=2, total_bytes=907000, hash_policy="none",
                      zero_byte_count=1, max_depth=3, skipped_count=0)
    cat.commit()
    cat.close()
    return TestClient(create_app(data_root=str(data_root)))


@pytest.fixture()
def client_sealed(tmp_path: Path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    _build_snapshot_db(data_root / "vol" / "20260101T000000Z" / "snapshot.db", sealed=True)
    with _make_client(data_root) as c:
        yield c


@pytest.fixture()
def client_legacy(tmp_path: Path):
    """旧库：封库产物但 stats_precomputed 表被删（模拟 P2-A 之前的快照）。"""
    data_root = tmp_path / "data"
    data_root.mkdir()
    db = data_root / "vol" / "20260101T000000Z" / "snapshot.db"
    _build_snapshot_db(db, sealed=True)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE stats_precomputed")
    conn.commit()
    conn.close()
    with _make_client(data_root) as c:
        yield c


# -------------------------------------------------------------- 封库落表


def test_seal_writes_stats_precomputed(tmp_path: Path) -> None:
    db = tmp_path / "snapshot.db"
    _build_snapshot_db(db, sealed=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    keys = {r["key"] for r in conn.execute("SELECT key FROM stats_precomputed")}
    assert keys == set(STATS_KEYS)
    # value_json 均为合法 JSON
    for r in conn.execute("SELECT value_json FROM stats_precomputed"):
        json.loads(r["value_json"])
    conn.close()


def test_seal_progress_total_includes_stats_step(tmp_path: Path) -> None:
    db = tmp_path / "snapshot.db"
    conn = sqlite3.connect(db)
    init_snapshot(conn)
    conn.commit()
    steps: "list[tuple[int, int]]" = []
    seal_snapshot(conn, progress=lambda d, t: steps.append((d, t)))
    conn.close()
    # 最后一步是 stats 预计算，total = max_depth(0) + 5
    assert steps[-1] == (5, 5)
    assert all(t == 5 for _, t in steps)


# -------------------------------------------------------------- 端点直读 / 回退


def test_stats_endpoint_reads_precomputed(client_sealed: TestClient) -> None:
    body = client_sealed.get(f"/api/snapshots/{SID}/stats").json()
    assert body["precomputed"] is True
    assert body["snapshot_id"] == SID
    assert body["zero_byte_count"] == 1
    assert body["top_files"][0]["name"] == "big.bin"


def test_stats_precomputed_equals_live(client_sealed: TestClient, tmp_path: Path) -> None:
    """预计算直读与实时聚合逐字段等值（含直方图逐桶）。"""
    pre = client_sealed.get(f"/api/snapshots/{SID}/stats").json()
    db = tmp_path / "data" / "vol" / "20260101T000000Z" / "snapshot.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    live = compute_stats(conn)
    conn.close()
    for key in STATS_KEYS:
        assert pre[key] == live[key], key


def test_stats_fallback_legacy_db(client_legacy: TestClient) -> None:
    """旧快照（无 stats_precomputed 表）→ 回退实时聚合，不报错。"""
    body = client_legacy.get(f"/api/snapshots/{SID}/stats").json()
    assert body["precomputed"] is False
    assert body["zero_byte_count"] == 1
    assert body["top_files"][0]["name"] == "big.bin"


def test_stats_fallback_partial_keys(client_sealed: TestClient, tmp_path: Path) -> None:
    """key 残缺（人为删一行）→ 回退实时聚合而非返回残缺结果。"""
    db = tmp_path / "data" / "vol" / "20260101T000000Z" / "snapshot.db"
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM stats_precomputed WHERE key='depth_histogram'")
    conn.commit()
    conn.close()
    assert load_precomputed_stats(sqlite3.connect(db)) is None
    body = client_sealed.get(f"/api/snapshots/{SID}/stats").json()
    assert body["precomputed"] is False
    assert {d["depth"]: d["count"] for d in body["depth_histogram"]} == {1: 2, 2: 2, 3: 1}


# -------------------------------------------------------------- build-stats CLI


def test_build_stats_cli_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "snapshot.db"
    _build_snapshot_db(db, sealed=True)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE stats_precomputed")
    conn.commit()
    conn.close()
    argv = ["build-stats", str(db)]
    assert cli_main(argv) == 0
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    first = {r["key"]: r["value_json"] for r in conn.execute("SELECT * FROM stats_precomputed")}
    conn.close()
    assert cli_main(argv) == 0  # 幂等：重复执行重建而非报错
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    second = {r["key"]: r["value_json"] for r in conn.execute("SELECT * FROM stats_precomputed")}
    conn.close()
    assert first == second
    assert set(first) == set(STATS_KEYS)


def test_build_stats_cli_unsealed_warns(tmp_path: Path, capsys) -> None:
    """未封库：仍可补建，但告警（对齐 build-fts 行为）。"""
    db = tmp_path / "snapshot.db"
    _build_snapshot_db(db, sealed=False)
    assert cli_main(["build-stats", str(db)]) == 0
    err = capsys.readouterr().err
    assert "未封库" in err


def test_build_stats_cli_missing_snapshot(tmp_path: Path) -> None:
    assert cli_main(["build-stats", "nope/db.db", "--data-root", str(tmp_path)]) == 2
