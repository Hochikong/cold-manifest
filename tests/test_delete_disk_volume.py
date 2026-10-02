"""磁盘/卷删除（后端）：先删快照与比对才能删盘/卷，不允许级联删快照。"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import (catalog_path, ensure_disk, ensure_volume,
                                   register_snapshot)
from cold_manifest.db import init_catalog
from cold_manifest.schema import CATALOG_DDL, SNAPSHOT_DDL
from cold_manifest.server import create_app


DISK = "SN123"
VOL = "SN123_P1"
A = f"{VOL}/20260101T000000Z"
B = f"{VOL}/20260202T000000Z"


def _mk_snapshot_db(data_root: Path, vol: str, ts: str) -> str:
    sid = f"{vol}/{ts}"
    db = data_root / vol / ts / "snapshot.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(SNAPSHOT_DDL)
    conn.execute("INSERT INTO meta(key, value) VALUES('status', 'sealed')")
    conn.execute("INSERT INTO meta(key, value) VALUES('volume_id', ?)", (vol,))
    conn.execute("INSERT INTO meta(key, value) VALUES('disk_serial', ?)", (DISK,))
    conn.commit()
    conn.close()
    return sid


def _seed_snapshots(conn: sqlite3.Connection) -> None:
    ensure_disk(conn, DISK)
    ensure_volume(conn, VOL, DISK)
    for sid in (A, B):
        register_snapshot(conn, sid, VOL, status="sealed", host_path="/t",
                          file_count=1, dir_count=0, total_bytes=5,
                          hash_policy="none")
    conn.commit()


@pytest.fixture()
def cat(tmp_path: Path) -> sqlite3.Connection:
    data_root = tmp_path / "data"
    data_root.mkdir()
    conn = sqlite3.connect(catalog_path(data_root))
    conn.row_factory = sqlite3.Row
    init_catalog(conn)
    yield conn
    conn.close()


@pytest.fixture()
def client(tmp_path: Path, cat: sqlite3.Connection) -> TestClient:
    _mk_snapshot_db(tmp_path / "data", VOL, "20260101T000000Z")
    _seed_snapshots(cat)
    with TestClient(create_app(data_root=str(tmp_path / "data"))) as c:
        yield c


@pytest.fixture()
def client_nosnap(tmp_path: Path, cat: sqlite3.Connection) -> TestClient:
    """只登记盘/卷，无任何快照。"""
    ensure_disk(cat, DISK)
    ensure_volume(cat, VOL, DISK)
    cat.commit()
    with TestClient(create_app(data_root=str(tmp_path / "data"))) as c:
        yield c


# ---------------------------------------------------------------- 有快照 → 409


def test_delete_volume_with_snapshots_blocked(client: TestClient) -> None:
    r = client.delete(f"/api/volumes/{VOL}")
    assert r.status_code == 409
    body = r.json()["detail"]
    assert "快照" in body["message"]
    assert sorted(body["snapshots"]) == [A, B]
    assert body["diffs"] == []
    # 快照与卷原封不动
    assert len(client.get("/api/snapshots").json()["items"]) == 2
    vols = client.get("/api/volumes").json()["items"]
    assert any(v["volume_id"] == VOL for v in vols)


def test_delete_disk_with_snapshots_blocked(client: TestClient) -> None:
    r = client.delete(f"/api/disks/{DISK}")
    assert r.status_code == 409
    body = r.json()["detail"]
    assert sorted(body["snapshots"]) == [A, B]
    # 盘与卷都没被删
    assert client.get(f"/api/disks/{DISK}").status_code == 200
    assert client.get(f"/api/volumes/{VOL}").status_code == 200


def test_blocked_lists_diff_ids(client: TestClient, cat: sqlite3.Connection) -> None:
    cat.execute(
        "INSERT INTO diff_runs(diff_id, a, b, options_hash, status) "
        "VALUES('d1', ?, ?, 'x', 'done')", (A, B))
    cat.commit()
    body = client.delete(f"/api/volumes/{VOL}").json()["detail"]
    assert body["diffs"] == ["d1"]


# ---------------------------------------------------------------- 无快照 → 删除成功


def test_delete_volume_ok(client_nosnap: TestClient, cat: sqlite3.Connection) -> None:
    r = client_nosnap.delete(f"/api/volumes/{VOL}")
    assert r.status_code == 200
    assert r.json() == {"volume_id": VOL, "deleted_snapshots": 0}
    # 列表不再出现，详情 404，再删同一个也 404
    vols = client_nosnap.get("/api/volumes").json()["items"]
    assert not any(v["volume_id"] == VOL for v in vols)
    assert client_nosnap.get(f"/api/volumes/{VOL}").status_code == 404
    assert client_nosnap.delete(f"/api/volumes/{VOL}").status_code == 404
    # 盘还在（只有卷被带走）
    assert client_nosnap.get(f"/api/disks/{DISK}").status_code == 200
    assert cat.execute("SELECT 1 FROM volumes WHERE volume_id=?",
                       (VOL,)).fetchone() is None


def test_delete_disk_takes_volumes(client_nosnap: TestClient,
                                   cat: sqlite3.Connection) -> None:
    ensure_volume(cat, "SN123_P2", DISK)
    cat.commit()
    r = client_nosnap.delete(f"/api/disks/{DISK}")
    assert r.status_code == 200
    body = r.json()
    assert body["disk_id"] == DISK
    assert body["deleted_volumes"] == 2
    assert body["deleted_snapshots"] == 0
    assert client_nosnap.get(f"/api/disks/{DISK}").status_code == 404
    assert client_nosnap.get(f"/api/volumes/{VOL}").status_code == 404
    assert client_nosnap.get("/api/volumes/SN123_P2").status_code == 404
    vols = client_nosnap.get("/api/volumes").json()["items"]
    assert not any(v["volume_id"].startswith("SN123") for v in vols)
    # 再删同一个 → 404
    assert client_nosnap.delete(f"/api/disks/{DISK}").status_code == 404


# ---------------------------------------------------------------- 404 / 400


def test_delete_unknown_404(client_nosnap: TestClient) -> None:
    assert client_nosnap.delete("/api/disks/NOPE").status_code == 404
    assert client_nosnap.delete("/api/volumes/NOPE_P1").status_code == 404


def test_delete_volume_invalid_id_400(client_nosnap: TestClient) -> None:
    # 注意：httpx 客户端会把 /..%2Fetc 规范化掉，走不到路由，这里只测字符集校验
    assert client_nosnap.delete("/api/volumes/has%20space_P1").status_code == 400
    assert client_nosnap.delete("/api/volumes/bad%21name").status_code == 400


# ---------------------------------------------------------------- 防御式清理


def test_defensive_cleanup_of_orphan_rows(tmp_path: Path) -> None:
    """on_disk_copies / disk_smart 的孤儿残留行随删除清掉。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(CATALOG_DDL)
    ensure_disk(conn, DISK)
    ensure_volume(conn, VOL, DISK)
    # 手工造孤儿行（正常流程不该出现）
    conn.execute(
        "INSERT INTO on_disk_copies(snapshot_id, disk_path, status) "
        "VALUES(?, '/x', 'ok')", (A,))
    conn.execute(
        "INSERT INTO disk_smart(disk_id, snapshot_id, health) "
        "VALUES(?, ?, 'passed')", (DISK, A))
    conn.commit()

    from cold_manifest.catalog import delete_disk, delete_volume
    # 卷下有"孤儿副本行"但没有快照行 → 不阻塞，副本行被清
    r = delete_volume(conn, tmp_path, VOL)
    assert r["deleted_snapshots"] == 0
    assert conn.execute("SELECT 1 FROM on_disk_copies").fetchone() is None

    # 重造盘/卷与 disk_smart 孤儿 → 删盘清掉
    ensure_volume(conn, VOL, DISK)
    conn.execute(
        "INSERT INTO disk_smart(disk_id, snapshot_id, health) "
        "VALUES(?, ?, 'passed')", (DISK, A))
    conn.commit()
    r = delete_disk(conn, tmp_path, DISK)
    assert r["deleted_volumes"] == 1
    assert conn.execute("SELECT 1 FROM disk_smart").fetchone() is None
    assert conn.execute("SELECT 1 FROM disks WHERE disk_id=?",
                        (DISK,)).fetchone() is None
