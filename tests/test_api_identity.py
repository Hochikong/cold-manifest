"""身份审计 API（PR-C）测试：/api/identity-audit 与 identity_status 派生。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import catalog_path, ensure_disk, ensure_volume, \
    register_snapshot
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.server import create_app

D_OK = "SNOKK111"       # 无任何回退证据 → unknown
D_FB = "SNFBB222"       # 有 fallback 证据 → unverified
D_CF = "SNCFG333"       # identity_verified=0（手工加列模拟 PR-B）→ conflict
V_OK = f"{D_OK}_P1"
V_FB = f"{D_FB}_P1"
V_CF = f"{D_CF}_P1"
SID_OK = f"{V_OK}/20260101T000000Z"
SID_FB = f"{V_FB}/20260101T000000Z"
SID_CF = f"{V_CF}/20260101T000000Z"


def _snap_db(data_root: Path, sid: str, meta: dict) -> None:
    p = data_root / sid.split("/")[0] / sid.split("/")[1] / "snapshot.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    init_snapshot(conn)
    conn.execute("INSERT INTO entries(entry_id, parent_id, path, name, depth, type,"
                 " size_bytes) VALUES(1, 0, '.', '', 0, 'dir', NULL)")
    for k, v in meta.items():
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?)", (k, v))
    conn.execute("INSERT INTO meta(key, value) VALUES('status', 'sealed')")
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    data_root = tmp_path / "data"
    data_root.mkdir()
    conn = sqlite3.connect(catalog_path(data_root))
    init_catalog(conn)
    for disk_id, vid in ((D_OK, V_OK), (D_FB, V_FB), (D_CF, V_CF)):
        ensure_disk(conn, disk_id)
        ensure_volume(conn, vid, disk_id)
    for sid in (SID_OK, SID_FB, SID_CF):
        register_snapshot(conn, sid, sid.split("/")[0], status="sealed",
                          host_path="/x", collected_at="2026-01-01T00:00:00Z")
    conn.commit()
    conn.close()
    _snap_db(data_root, SID_OK, {"disk_serial": D_OK})
    _snap_db(data_root, SID_FB, {
        "disk_serial": D_FB,
        "smart_scan_info_json": json.dumps({
            "candidates": [{"device": "/dev/sdb", "type": "", "source": "fallback-sd"}],
            "device_used": "/dev/sdb", "mapped_from_scan": False})})
    _snap_db(data_root, SID_CF, {"disk_serial": D_CF})
    # identity_verified / identity_conflict_json 列已由 PR-B 落地，直接写值
    conn = sqlite3.connect(catalog_path(data_root))
    conn.execute("UPDATE disks SET identity_verified=0 WHERE disk_id=?", (D_CF,))
    conn.commit()
    conn.close()
    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c


def test_identity_audit_endpoint(client: TestClient) -> None:
    r = client.get("/api/identity-audit")
    assert r.status_code == 200
    body = r.json()
    assert set(body["summary"]) == {"high", "medium", "low"}
    assert set(body) >= {"alerts", "affected_disk_ids", "summary", "warnings",
                         "notes"}
    assert isinstance(body["alerts"], list)


def test_disk_identity_status_derivation(client: TestClient) -> None:
    r = client.get("/api/disks")
    assert r.status_code == 200
    by_id = {d["disk_id"]: d for d in r.json()["items"]}
    assert by_id[D_OK]["identity_status"] == "unknown"
    assert by_id[D_FB]["identity_status"] == "unverified"
    assert "回退" in by_id[D_FB]["identity_status_reason"]
    assert by_id[D_CF]["identity_status"] == "conflict"


def test_disk_detail_identity_status(client: TestClient) -> None:
    r = client.get(f"/api/disks/{D_FB}")
    assert r.status_code == 200
    assert r.json()["identity_status"] == "unverified"


def test_snapshot_list_and_detail_carry_identity_status(client: TestClient) -> None:
    r = client.get("/api/snapshots")
    assert r.status_code == 200
    by_sid = {s["snapshot_id"]: s for s in r.json()["items"]}
    assert by_sid[SID_OK]["identity_status"] == "unknown"
    assert by_sid[SID_FB]["identity_status"] == "unverified"

    r = client.get(f"/api/snapshots/{SID_FB}")
    assert r.status_code == 200
    assert r.json()["identity_status"] == "unverified"
