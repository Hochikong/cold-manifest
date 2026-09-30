"""P4-⑧ import-db：就地登记 snapshot.db 到 catalog。"""

import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import connect_catalog, snapshot_path
from cold_manifest.cli import main
from cold_manifest.collect import collect_volume
from cold_manifest.import_db import ImportDbError, import_snapshot_db
from cold_manifest.schema import SNAPSHOT_DDL
from cold_manifest.server import create_app


def _fake_probe():
    from cold_manifest.probe import DiskInfo, VolumeInfo
    vol = VolumeInfo(
        filesystem="ext4", label="IMPLBL", volume_serial_hex="abcd-1234",
        partition_uuid="1111-2222", partition_index=1, partition_table_type="GPT",
        capacity_bytes=10_000_000_000, free_bytes=9_000_000_000,
        mount_point="/mnt/fake", device_path="/dev/sdb1",
    )
    disk = DiskInfo(
        physical_model="Fake Disk 5000", physical_serial="SERIMP123",
        disk_serial="SERIMP123", serial_source="probe",
        bridge_model="", interface_type="USB", capacity_bytes=20_000_000_000,
        firmware="fw1", smart_status="unavailable",
    )
    return lambda path, *, manual_serial=None, smartctl=True: (vol, disk)


@pytest.fixture()
def collected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("cold_manifest.collect.probe_path", _fake_probe())
    scan_root = tmp_path / "vol"
    (scan_root / "docs").mkdir(parents=True)
    (scan_root / "docs" / "a.txt").write_bytes(b"x" * 100)
    (scan_root / "b.bin").write_bytes(b"y" * 200)
    data_root = tmp_path / "data"
    result = collect_volume(scan_root, data_root=data_root, on_disk_copy=False)
    return tmp_path, data_root, result.snapshot_id


def test_import_db_registers_snapshot(collected) -> None:
    tmp_path, data_root, sid = collected
    # 新数据根：只带库文件（模拟 catalog 丢失后重新登记）
    other_root = tmp_path / "other"
    vol_id, ts = sid.split("/")
    dest = other_root / vol_id / ts
    dest.mkdir(parents=True)
    shutil.copy2(data_root / sid / "snapshot.db", dest / "snapshot.db")

    result = import_snapshot_db(dest / "snapshot.db", other_root)
    assert result["created"] is True
    assert result["snapshot_id"] == sid

    cat = connect_catalog(other_root)
    try:
        row = cat.execute("SELECT * FROM snapshots WHERE snapshot_id=?",
                          (sid,)).fetchone()
        assert row is not None
        assert row["file_count"] == 2
        assert row["total_bytes"] == 300
        assert row["status"] == "sealed"
        disk = cat.execute(
            "SELECT d.disk_id FROM volumes v JOIN disks d ON d.disk_id=v.disk_id"
            " WHERE v.volume_id=?", (vol_id,)).fetchone()
        assert disk["disk_id"] == "SERIMP123"
    finally:
        cat.close()


def test_import_db_idempotent_updates(collected) -> None:
    tmp_path, data_root, sid = collected
    db = data_root / sid / "snapshot.db"
    first = import_snapshot_db(db, data_root)
    assert first["created"] is False  # collect 已注册
    second = import_snapshot_db(db, data_root)
    assert second["created"] is False
    cat = connect_catalog(data_root)
    try:
        n = cat.execute("SELECT COUNT(*) FROM snapshots WHERE snapshot_id=?",
                        (sid,)).fetchone()[0]
        assert n == 1
        assert cat.execute("SELECT file_count FROM snapshots WHERE snapshot_id=?",
                           (sid,)).fetchone()[0] == 2
    finally:
        cat.close()


def test_import_db_missing_host_path_warns(collected) -> None:
    tmp_path, data_root, sid = collected
    db = data_root / sid / "snapshot.db"
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM meta WHERE key IN ('scan_root','root_path')")
    conn.commit()
    conn.close()
    result = import_snapshot_db(db, data_root)
    assert any("scan_root" in w for w in result["warnings"])
    cat = connect_catalog(data_root)
    try:
        assert cat.execute("SELECT host_path FROM snapshots WHERE snapshot_id=?",
                           (sid,)).fetchone()[0] == ""
    finally:
        cat.close()


def test_import_db_errors(collected) -> None:
    tmp_path, data_root, sid = collected
    with pytest.raises(ImportDbError):
        import_snapshot_db(tmp_path / "nope.db", data_root)
    # 未封库
    bad = tmp_path / "volbad" / "20200101T000000Z"
    bad.mkdir(parents=True)
    conn = sqlite3.connect(str(bad / "snapshot.db"))
    conn.executescript(SNAPSHOT_DDL)
    conn.execute("INSERT INTO meta VALUES('status','scanning')")
    conn.commit()
    conn.close()
    with pytest.raises(ImportDbError):
        import_snapshot_db(bad / "snapshot.db", data_root)


def test_cli_import_db_and_api_visibility(collected) -> None:
    tmp_path, data_root, sid = collected
    other_root = tmp_path / "other2"
    vol_id, ts = sid.split("/")
    dest = other_root / vol_id / ts
    dest.mkdir(parents=True)
    shutil.copy2(data_root / sid / "snapshot.db", dest / "snapshot.db")

    rc = main(["import-db", str(dest / "snapshot.db"),
               "--data-root", str(other_root)])
    assert rc == 0

    # --copy 未实现 → 退出码 2
    rc = main(["import-db", str(dest / "snapshot.db"), "--copy",
               "--data-root", str(other_root)])
    assert rc == 2

    from cold_manifest.db import init_catalog  # noqa: F401
    client = TestClient(create_app(data_root=str(other_root)))
    with client as c:
        resp = c.get("/api/snapshots")
        assert resp.status_code == 200
        ids = [it["snapshot_id"] for it in resp.json()["items"]]
        assert sid in ids
        detail = c.get(f"/api/snapshots/{sid}")
        assert detail.status_code == 200
        assert detail.json()["file_count"] == 2
