"""P4-⑥ rebuild-catalog：扫描快照库 meta 补建/回填 catalog 注册行。"""

import sqlite3
from pathlib import Path

import pytest

from cold_manifest.catalog import connect_catalog
from cold_manifest.cli import main
from cold_manifest.collect import collect_volume
from cold_manifest.probe import DiskInfo, VolumeInfo
from cold_manifest.rebuild import rebuild_catalog


def _fake_probe():
    vol = VolumeInfo(
        filesystem="ext4", label="RBLBL", volume_serial_hex="abcd-1234",
        partition_uuid="1111-2222", partition_index=1, partition_table_type="GPT",
        capacity_bytes=10_000_000_000, free_bytes=9_000_000_000,
        mount_point="/mnt/fake", device_path="/dev/sdb1",
    )
    disk = DiskInfo(
        physical_model="Fake Disk 5000", physical_serial="SERRB123",
        disk_serial="SERRB123", serial_source="probe",
        bridge_model="", interface_type="USB", capacity_bytes=20_000_000_000,
        firmware="fw1", smart_status="unavailable",
    )
    return lambda path, *, manual_serial=None, smartctl=True: (vol, disk)


@pytest.fixture()
def collected(tmp_path: Path, monkeypatch) -> Path:
    """collect 一次得到 data_root（catalog 行齐全）。"""
    monkeypatch.setattr("cold_manifest.collect.probe_path", _fake_probe())
    scan_root = tmp_path / "vol"
    (scan_root / "docs").mkdir(parents=True)
    (scan_root / "docs" / "a.txt").write_bytes(b"x" * 100)
    (scan_root / "b.bin").write_bytes(b"y" * 200)
    data_root = tmp_path / "data"
    collect_volume(scan_root, data_root=data_root, on_disk_copy=False)
    return data_root


def _wipe_registry(data_root: Path) -> None:
    """模拟 catalog 注册行丢失（diff_runs/tasks 等不动）。"""
    cat = connect_catalog(data_root)
    try:
        cat.execute("DELETE FROM on_disk_copies")
        cat.execute("DELETE FROM snapshots")
        cat.execute("DELETE FROM batches")
        cat.execute("DELETE FROM volumes")
        cat.execute("DELETE FROM disks")
        cat.commit()
    finally:
        cat.close()


def _registry_rows(data_root: Path) -> dict:
    cat = connect_catalog(data_root)
    try:
        return {
            t: cat.execute(f"SELECT * FROM {t}").fetchall()
            for t in ("disks", "volumes", "snapshots", "on_disk_copies")
        }
    finally:
        cat.close()


def test_rebuild_restores_missing_rows(collected: Path) -> None:
    _wipe_registry(collected)
    result = rebuild_catalog(collected)
    assert result["scanned"] == 1
    assert result["disks_added"] == 1
    assert result["volumes_added"] == 1
    assert result["snapshots_added"] == 1
    rows = _registry_rows(collected)
    assert rows["disks"][0]["disk_id"] == "SERRB123"
    snap = rows["snapshots"][0]
    assert snap["file_count"] == 2
    assert snap["total_bytes"] == 300
    assert snap["status"] == "sealed"
    assert snap["host_path"]  # meta scan_root 回填


def test_rebuild_is_idempotent(collected: Path) -> None:
    rebuild_catalog(collected)
    result = rebuild_catalog(collected)
    assert result["disks_added"] == 0
    assert result["volumes_added"] == 0
    assert result["snapshots_added"] == 0
    assert result["snapshots_skipped"] == 1
    assert result["fields_backfilled"] == 0


def test_rebuild_never_overwrites_existing_values(collected: Path) -> None:
    cat = connect_catalog(collected)
    try:
        cat.execute("UPDATE disks SET physical_model='USER-MARKED'")
        cat.execute("UPDATE snapshots SET file_count=999")
        cat.commit()
    finally:
        cat.close()
    rebuild_catalog(collected)
    cat = connect_catalog(collected)
    try:
        assert cat.execute("SELECT physical_model FROM disks").fetchone()[0] \
            == "USER-MARKED"
        assert cat.execute("SELECT file_count FROM snapshots").fetchone()[0] == 999
    finally:
        cat.close()


def test_rebuild_backfills_null_fields_only(collected: Path) -> None:
    cat = connect_catalog(collected)
    try:
        cat.execute("UPDATE disks SET physical_model=NULL, interface_type=NULL")
        cat.execute("UPDATE snapshots SET collector_version=NULL")
        cat.commit()
    finally:
        cat.close()
    result = rebuild_catalog(collected)
    assert result["fields_backfilled"] >= 3
    cat = connect_catalog(collected)
    try:
        assert cat.execute("SELECT physical_model FROM disks").fetchone()[0] \
            == "Fake Disk 5000"
        assert cat.execute(
            "SELECT collector_version FROM snapshots").fetchone()[0] is not None
    finally:
        cat.close()


def test_rebuild_dry_run_writes_nothing(collected: Path) -> None:
    _wipe_registry(collected)
    result = rebuild_catalog(collected, dry_run=True)
    assert result["snapshots_added"] == 1
    rows = _registry_rows(collected)
    assert not rows["disks"] and not rows["snapshots"]


def test_rebuild_skips_unsealed_and_corrupt(collected: Path) -> None:
    # 未封库
    bad_dir = collected / "VOLX_P1" / "20200101T000000Z"
    bad_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(bad_dir / "snapshot.db"))
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO meta VALUES('status', 'scanning')")
    conn.commit()
    conn.close()
    # 损坏
    corrupt_dir = collected / "VOLY_P1" / "20200101T000000Z"
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / "snapshot.db").write_bytes(b"not a database" * 10)
    # 非时间戳目录
    weird_dir = collected / "VOLZ_P1" / "notats"
    weird_dir.mkdir(parents=True)
    (weird_dir / "snapshot.db").write_bytes(b"whatever")

    result = rebuild_catalog(collected)
    assert result["scanned"] == 1
    joined = "\n".join(result["warnings"])
    assert "未封库" in joined
    assert "损坏" in joined
    assert "非时间戳" in joined


def test_cli_rebuild_catalog(collected: Path, capsys) -> None:
    _wipe_registry(collected)
    rc = main(["rebuild-catalog", "--data-root", str(collected)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "快照 1" in out
    rc = main(["rebuild-catalog", "--data-root", str(collected), "--dry-run"])
    assert rc == 0
