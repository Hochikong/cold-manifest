"""P4-⑦ 快照 HTML 报告：generate_snapshot_report + CLI + API 端点。"""

import io
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import (connect_catalog, ensure_disk, ensure_volume,
                                   register_snapshot)
from cold_manifest.cli import main
from cold_manifest.collect import collect_volume
from cold_manifest.db import open_snapshot
from cold_manifest.probe import DiskInfo, VolumeInfo
from cold_manifest.report import generate_snapshot_report
from cold_manifest.server import create_app


def _fake_probe():
    vol = VolumeInfo(
        filesystem="ext4", label="REPLBL", volume_serial_hex="abcd-1234",
        partition_uuid="1111-2222", partition_index=1, partition_table_type="GPT",
        capacity_bytes=10_000_000_000, free_bytes=9_000_000_000,
        mount_point="/mnt/fake", device_path="/dev/sdb1",
    )
    disk = DiskInfo(
        physical_model="Fake Disk 5000", physical_serial="SERREP123",
        disk_serial="SERREP123", serial_source="probe",
        bridge_model="", interface_type="SATA", capacity_bytes=20_000_000_000,
        firmware="fw1", smart_status="unavailable",
    )
    return lambda path, *, manual_serial=None, smartctl=True: (vol, disk)


@pytest.fixture()
def snapshot_env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("cold_manifest.collect.probe_path", _fake_probe())
    scan_root = tmp_path / "vol"
    (scan_root / "docs").mkdir(parents=True)
    (scan_root / "docs" / "a.txt").write_bytes(b"x" * 100)
    (scan_root / "docs" / "b.log").write_bytes(b"y" * 200)
    (scan_root / "c.bin").write_bytes(b"z" * 50)
    (scan_root / "emptydir").mkdir()
    (scan_root / "skipme").mkdir()
    (scan_root / "skipme" / "z.log").write_bytes(b"w" * 10)
    data_root = tmp_path / "data"
    result = collect_volume(scan_root, data_root=data_root, on_disk_copy=False)
    return data_root, result.snapshot_id


def _render(data_root: Path, snapshot_id: str, **kw) -> tuple[str, dict]:
    conn = open_snapshot(data_root / snapshot_id / "snapshot.db")
    try:
        buf = io.StringIO()
        summary = generate_snapshot_report(conn, buf, snapshot_id=snapshot_id, **kw)
        return buf.getvalue(), summary
    finally:
        conn.close()


def test_report_contains_core_sections(snapshot_env) -> None:
    data_root, sid = snapshot_env
    html, summary = _render(data_root, sid)
    assert summary["file_count"] == 4
    assert summary["dir_count"] == 3  # docs/emptydir/skipme
    assert summary["total_bytes"] == 360
    assert summary["skipped_total"] == 0
    for needle in ("扩展名 Top", "大小直方图", "深度分布", "顶层目录 Top",
                   "跳过项摘要", ".txt", sid):
        assert needle in html


def test_report_counts_match_sql(snapshot_env) -> None:
    data_root, sid = snapshot_env
    conn = sqlite3.connect(str(data_root / sid / "snapshot.db"))
    files = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size_bytes),0) FROM entries"
        " WHERE type='file'").fetchone()
    skipped = conn.execute("SELECT COUNT(*) FROM skipped").fetchone()[0]
    conn.close()
    _, summary = _render(data_root, sid)
    assert (summary["file_count"], summary["total_bytes"]) == files
    assert summary["skipped_total"] == skipped


def test_report_truncation_marker(snapshot_env) -> None:
    data_root, sid = snapshot_env
    html, _ = _render(data_root, sid, max_rows_per_section=2)
    assert "已截断" in html


def test_report_with_catalog_rows_and_smart(snapshot_env) -> None:
    data_root, sid = snapshot_env
    cat = connect_catalog(data_root)
    try:
        row = cat.execute("SELECT * FROM snapshots WHERE snapshot_id=?",
                          (sid,)).fetchone()
        vol = cat.execute("SELECT * FROM volumes").fetchone()
        cat.execute(
            "INSERT INTO disk_smart(disk_id, snapshot_id, collected_at, health,"
            " temperature_c, power_on_hours) VALUES(?,?,?,?,?,?)",
            (vol["disk_id"], sid, row["collected_at"], "passed", 31, 12345))
        cat.commit()
        smart = cat.execute("SELECT * FROM disk_smart").fetchone()
    finally:
        cat.close()
    html, _ = _render(data_root, sid, snapshot_row=row, volume_row=vol,
                      smart_row=smart)
    assert "健康：passed" in html
    assert "31°C" in html
    assert "REPLBL" in html


def test_cli_report_writes_file(snapshot_env, tmp_path: Path, capsys) -> None:
    data_root, sid = snapshot_env
    out_path = tmp_path / "report.html"
    rc = main(["report", sid, "--html", str(out_path),
               "--data-root", str(data_root)])
    assert rc == 0
    assert out_path.is_file()
    html = out_path.read_text(encoding="utf-8")
    assert "快照报告" in html
    capsys.readouterr()
    # 缺 --html → 退出码 2
    rc = main(["report", sid, "--data-root", str(data_root)])
    assert rc == 2


def _make_client(data_root: Path, snapshot_id: str) -> TestClient:
    cat = connect_catalog(data_root)
    try:
        ensure_disk(cat, "DISKR", physical_model="T", capacity_bytes=2000)
        ensure_volume(cat, "volr", "DISKR", filesystem="NTFS")
        register_snapshot(cat, snapshot_id, "volr", status="sealed",
                          host_path="/test", file_count=1, dir_count=0,
                          total_bytes=5, hash_policy="none",
                          zero_byte_count=0, max_depth=1, skipped_count=0)
        cat.commit()
    finally:
        cat.close()
    return TestClient(create_app(data_root=str(data_root)))


def _build_snapshot_db(db: Path) -> None:
    from cold_manifest.schema import SNAPSHOT_DDL
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(SNAPSHOT_DDL)
    conn.execute("INSERT INTO meta VALUES('status','sealed')")
    conn.execute(
        "INSERT INTO entries(parent_id,path,name,depth,type,size_bytes,ext)"
        " VALUES(0,'.','.',0,'dir',NULL,'')")
    conn.execute(
        "INSERT INTO entries(parent_id,path,name,depth,type,size_bytes,ext)"
        " VALUES(1,'a.txt','a.txt',1,'file',5,'.txt')")
    conn.commit()
    conn.close()


def test_api_report_html_and_errors(tmp_path: Path) -> None:
    sid = "volr/20260101T000000Z"
    _build_snapshot_db(tmp_path / "volr" / "20260101T000000Z" / "snapshot.db")
    client = _make_client(tmp_path, sid)
    with client as c:
        resp = c.get(f"/api/snapshots/{sid}/report")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "快照报告" in resp.text
        assert c.get(f"/api/snapshots/{sid}/report?format=csv").status_code == 400
        assert c.get(
            "/api/snapshots/nope/20260101T000000Z/report").status_code == 404
