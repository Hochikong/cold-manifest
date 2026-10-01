"""外部 snapshot.db 就地登记（POST /api/imports/db）与 catalog 重建
（POST /api/admin/rebuild-catalog）端点测试（P4.5-fix-60）。"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import catalog_path, ensure_disk, ensure_volume
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.lockfile import DataRootLock
from cold_manifest.server import create_app

_META_SEALED = {
    "status": "sealed",
    "volume_id": "volx",
    "collect_time_utc": "2026-05-01T00:00:00Z",
    "hash_policy": "none",
}


def _make_db(path: Path, meta: "dict[str, str]") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    init_snapshot(conn)
    conn.executemany("INSERT INTO meta(key, value) VALUES(?,?)",
                     list(meta.items()))
    conn.executemany(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type,"
        " size_bytes, mtime_ns) VALUES(?,?,?,?,?,?,?,?)",
        [(1, 0, ".", "", 0, "dir", None, None),
         (2, 1, "a.txt", "a.txt", 1, "file", 100, 100)])
    conn.commit()
    conn.close()


@pytest.fixture()
def env(tmp_path: Path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    ext = tmp_path / "ext" / "volx" / "20260501T000000Z"
    _make_db(ext / "snapshot.db", _META_SEALED)
    # 数据根里一个未登记的库（供 rebuild）
    _make_db(data_root / "voly" / "20260601T000000Z" / "snapshot.db", {
        "status": "sealed", "volume_id": "voly",
        "collect_time_utc": "2026-06-01T00:00:00Z", "hash_policy": "none"})
    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c, data_root, ext / "snapshot.db"


# ---------------------------------------------------------------- imports/db


def test_import_db_success(env) -> None:
    client, _, db_path = env
    r = client.post("/api/imports/db", json={"path": str(db_path)})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] is True
    assert body["snapshot_id"] == "volx/20260501T000000Z"
    assert body["snapshot"]["status"] == "sealed"
    # /api/snapshots 可见
    r2 = client.get("/api/snapshots")
    assert "volx/20260501T000000Z" in [s["snapshot_id"] for s in r2.json()["items"]]


def test_import_db_idempotent(env) -> None:
    client, _, db_path = env
    assert client.post("/api/imports/db", json={"path": str(db_path)}).status_code == 201
    r = client.post("/api/imports/db", json={"path": str(db_path)})
    assert r.status_code == 200
    assert r.json()["created"] is False


def test_import_db_unsealed_400(env, tmp_path: Path) -> None:
    client, _, _ = env
    bad = tmp_path / "ext" / "volx" / "20260502T000000Z" / "snapshot.db"
    _make_db(bad, {**_META_SEALED, "status": "collecting"})
    r = client.post("/api/imports/db", json={"path": str(bad)})
    assert r.status_code == 400
    assert "未封库" in r.json()["detail"]


def test_import_db_not_found_and_relative_400(env) -> None:
    client, _, _ = env
    assert client.post("/api/imports/db",
                       json={"path": "/no/such/db.sqlite"}).status_code == 400
    assert client.post("/api/imports/db",
                       json={"path": "relative/db.sqlite"}).status_code == 400


def test_import_db_outside_whitelist_400(env, monkeypatch) -> None:
    client, _, db_path = env
    monkeypatch.setenv("CLDM_IMPORT_ROOTS", "/somewhere/else")
    r = client.post("/api/imports/db", json={"path": str(db_path)})
    assert r.status_code == 400
    assert "导入根" in r.json()["detail"]


# ---------------------------------------------------------------- admin


def test_rebuild_dry_run(env) -> None:
    client, _, _ = env
    r = client.post("/api/admin/rebuild-catalog", json={"dry_run": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["snapshots_added"] == 1
    assert body["scanned"] == 1
    # dry_run 不落库：快照仍未注册
    assert client.get("/api/snapshots/voly/20260601T000000Z").status_code == 404


def test_rebuild_applies(env) -> None:
    client, _, _ = env
    r = client.post("/api/admin/rebuild-catalog", json={})
    assert r.status_code == 200
    assert r.json()["snapshots_added"] == 1
    assert client.get("/api/snapshots/voly/20260601T000000Z").status_code == 200
    # 幂等：重跑 added=0
    r2 = client.post("/api/admin/rebuild-catalog", json={})
    assert r2.json()["snapshots_added"] == 0


def test_rebuild_lock_busy_409(env, monkeypatch) -> None:
    client, data_root, _ = env
    monkeypatch.delenv("CLDM_SKIP_LOCK", raising=False)
    with DataRootLock(data_root):
        r = client.post("/api/admin/rebuild-catalog", json={})
    assert r.status_code == 409
    assert "被占用" in r.json()["detail"]
