"""DELETE /api/diffs/{diff_id} 端点测试：删物化库 + catalog 行，running 409、404 幂等。"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import catalog_path, register_snapshot, snapshot_path
from cold_manifest.db import init_catalog
from cold_manifest.schema import SNAPSHOT_DDL
from cold_manifest.server import create_app

from test_api_diff import _mk_snapshot

A = "VOL_P0/20260101T000000Z"
B = "VOL_P0/20260202T000000Z"

ROWS = [
    ("docs/same.txt", "file", 10, 111),
    ("new/only_b.txt", "file", 3, 222),
]


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _mk_snapshot(data_root, "VOL_P0", "20260101T000000Z", ROWS)
    _mk_snapshot(data_root, "VOL_P0", "20260202T000000Z", ROWS)

    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    for sid in (A, B):
        register_snapshot(cat, sid, "VOL_P0", status="sealed", host_path="/test",
                          file_count=3, dir_count=1, total_bytes=13, hash_policy="none")
    cat.commit()
    cat.close()

    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c


@pytest.fixture()
def diff_id(client: TestClient) -> str:
    r = client.post("/api/diffs", json={"a": A, "b": B})
    assert r.status_code == 200
    return r.json()["diff_id"]


def _catalog(data_root: Path, diff_id: str) -> sqlite3.Row | None:
    cat = sqlite3.connect(catalog_path(data_root))
    cat.row_factory = sqlite3.Row
    try:
        return cat.execute(
            "SELECT diff_id FROM diff_runs WHERE diff_id=?", (diff_id,)).fetchone()
    finally:
        cat.close()


def test_delete_diff_ok(client: TestClient, diff_id: str, tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    db = data_root / "_diffs" / f"{diff_id}.db"
    assert db.is_file()
    assert client.get(f"/api/diffs/{diff_id}").status_code == 200

    r = client.delete(f"/api/diffs/{diff_id}")
    assert r.status_code == 200
    assert r.json() == {"diff_id": diff_id, "deleted": True}
    # 物化库已删、catalog 行已删
    assert not db.exists()
    assert _catalog(data_root, diff_id) is None
    # 列表不再出现
    ids = [i["diff_id"] for i in client.get("/api/diffs").json()["items"]]
    assert diff_id not in ids
    # 重复删除 → 404（未登记）
    assert client.delete(f"/api/diffs/{diff_id}").status_code == 404
    # 明细 / entries 也 404
    assert client.get(f"/api/diffs/{diff_id}").status_code == 404


def test_delete_diff_unknown(client: TestClient) -> None:
    assert client.delete("/api/diffs/" + "0" * 16).status_code == 404


def test_delete_diff_running_409(client: TestClient, diff_id: str, tmp_path: Path) -> None:
    cat = sqlite3.connect(catalog_path(tmp_path / "data"))
    cat.execute("UPDATE diff_runs SET status='running' WHERE diff_id=?", (diff_id,))
    cat.commit()
    cat.close()
    r = client.delete(f"/api/diffs/{diff_id}")
    assert r.status_code == 409
    # 现场未动
    assert _catalog(tmp_path / "data", diff_id) is not None
    assert (tmp_path / "data" / "_diffs" / f"{diff_id}.db").is_file()


def test_delete_diff_missing_db_still_removes_row(client: TestClient, tmp_path: Path) -> None:
    """物化库被手工清掉（或从未落盘）→ 仍幂等删 catalog 行。"""
    (tmp_path / "data" / "_diffs").mkdir(exist_ok=True)
    cat = sqlite3.connect(catalog_path(tmp_path / "data"))
    cat.execute(
        "INSERT INTO diff_runs(diff_id, a, b, options_hash, options_json, created_at,"
        " duration_ms, status, summary_json, result_path)"
        " VALUES('ghostdiff0001', ?, ?, 'h', '{}', '2026-01-01T00:00:00Z', 0, 'done', NULL, NULL)",
        (A, B))
    cat.commit()
    cat.close()
    r = client.delete("/api/diffs/ghostdiff0001")
    assert r.status_code == 200
    assert r.json() == {"diff_id": "ghostdiff0001", "deleted": True}
    assert _catalog(tmp_path / "data", "ghostdiff0001") is None
