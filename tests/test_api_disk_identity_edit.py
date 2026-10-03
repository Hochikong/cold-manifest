"""Web 端磁盘身份修正/确认入口：PATCH /api/disks/{id} 扩展 + POST identity/recheck。

- PATCH：identity_verified（manual 来源+时间戳）、physical_serial、
  disk_serial（无快照级联更名 / 有快照 400）；
- recheck：打桩 attached 枚举 + resolve_smart_device + read_smart_verbose，
  覆盖 verified / unverified / not_attached 三态。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import (ensure_disk, ensure_volume,
                                   register_snapshot)
from cold_manifest.db import init_catalog
from cold_manifest.schema import SNAPSHOT_DDL
from cold_manifest.server import create_app

DISK, VOL = "SN123", "SN123_P1"
A = f"{VOL}/20260101T000000Z"
FREE = "FREESN"  # 无快照的盘


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


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    data_root = tmp_path / "data"
    data_root.mkdir()
    conn = sqlite3.connect(str(data_root / "catalog.db"))
    conn.row_factory = sqlite3.Row
    init_catalog(conn)
    _mk_snapshot_db(data_root, VOL, "20260101T000000Z")
    ensure_disk(conn, DISK)
    ensure_volume(conn, VOL, DISK)
    register_snapshot(conn, A, VOL, status="sealed", host_path="/t",
                      file_count=0, dir_count=0, total_bytes=0,
                      hash_policy="none")
    ensure_disk(conn, FREE)
    conn.commit()
    conn.close()
    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c


def _disk_row(client: TestClient, disk_id: str) -> dict:
    r = client.get(f"/api/disks/{disk_id}")
    assert r.status_code == 200
    return r.json()


# ---------------------------------------------------------------- PATCH identity_verified


def test_patch_identity_verified_manual(client: TestClient) -> None:
    r = client.patch(f"/api/disks/{DISK}", json={"identity_verified": True})
    assert r.status_code == 200
    body = r.json()
    assert body["identity_verified"] == 1
    assert body["identity_verified_source"] == "manual"
    assert body["identity_verified_at"]
    assert body["identity_status"] == "verified"

    # 显式 false → 0，来源仍是 manual
    r = client.patch(f"/api/disks/{DISK}", json={"identity_verified": False})
    body = r.json()
    assert body["identity_verified"] == 0
    assert body["identity_verified_source"] == "manual"
    assert body["identity_status"] == "conflict"

    # GET 列表 / 详情均带出新字段
    lst = client.get("/api/disks").json()["items"]
    item = next(i for i in lst if i["disk_id"] == DISK)
    assert item["identity_verified_source"] == "manual"
    assert item["identity_verified_at"]
    assert _disk_row(client, DISK)["identity_verified_source"] == "manual"

    # 未知盘 → 404
    assert client.patch("/api/disks/NOPE",
                        json={"identity_verified": True}).status_code == 404


# ---------------------------------------------------------------- PATCH physical_serial


def test_patch_physical_serial(client: TestClient, tmp_path: Path) -> None:
    r = client.patch(f"/api/disks/{DISK}",
                     json={"physical_serial": "BOX9900", "nickname": "备份盘"})
    assert r.status_code == 200
    body = r.json()
    assert body["physical_serial"] == "BOX9900"
    assert body["nickname"] == "备份盘"  # nickname 不受影响

    # 空串 = 清除
    assert client.patch(f"/api/disks/{DISK}",
                        json={"physical_serial": ""}).json()["physical_serial"] is None
    # 超长 → 400
    assert client.patch(f"/api/disks/{DISK}",
                        json={"physical_serial": "x" * 129}).status_code == 400


# ---------------------------------------------------------------- PATCH disk_serial


def test_patch_disk_serial_no_snapshots(client: TestClient) -> None:
    r = client.patch(f"/api/disks/{FREE}", json={"disk_serial": "NEWSER"})
    assert r.status_code == 200
    assert r.json()["disk_id"] == "NEWSER"

    # 旧盘号已不存在，新盘号可查且卷已随之更名
    assert client.get("/api/disks/FREESN").status_code == 404
    row = client.get("/api/disks/NEWSER").json()
    assert row["disk_id"] == "NEWSER"
    assert row["physical_serial"] is None


def test_patch_disk_serial_with_snapshots_400(client: TestClient) -> None:
    r = client.patch(f"/api/disks/{DISK}", json={"disk_serial": "NEWSER"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "快照" in detail and "重新校验" in detail
    # 盘记录未被改动
    assert _disk_row(client, DISK)["disk_id"] == DISK


# ---------------------------------------------------------------- recheck


class _Stub:
    """打桩 attached 枚举 + 定位 + SMART 读取。"""

    def __init__(self, monkeypatch, *, attached, raw=None, ok=True,
                 mapped_by="serial_match", message=""):
        import cold_manifest.api.routes_disks as rd
        import cold_manifest.smart as smart

        self.captured: dict = {}

        def fake_resolve(**kw):
            self.captured["resolve"] = kw
            return ([{"device": "/dev/sdx", "type": "", "source": "attached"}],
                    {"mapped_by": mapped_by})

        def fake_read(devices=None, **kw):
            self.captured["devices"] = devices
            return {"ok": ok, "raw": raw, "exit_status": 0,
                    "reason": "" if ok else "io_error",
                    "message": message, "device_type": "sat",
                    "raw_excerpt": "", "attempts": [],
                    "device_candidates": None, "scan_info": {}}

        monkeypatch.setattr(rd, "_attached_linux",
                            lambda: {"available": True, "items": attached,
                                     "count": len(attached)})
        monkeypatch.setattr(smart, "resolve_smart_device", fake_resolve)
        monkeypatch.setattr(smart, "read_smart_verbose", fake_read)


def _attached_item(serial: str) -> dict:
    return {"device": f"\\\\.\\PhysicalDrive0", "serial": serial,
            "model": "TestDisk", "size_bytes": 500107862016, "volumes": []}


_RAW_SN123 = json.dumps({"serial_number": "SN123",
                         "smart_status": {"passed": True},
                         "user_capacity": {"bytes": 500107862016}})
_RAW_OTHER = json.dumps({"serial_number": "OTHERSER",
                         "smart_status": {"passed": True}})


def test_recheck_verified(client: TestClient, monkeypatch) -> None:
    _Stub(monkeypatch, attached=[_attached_item("SN123")], raw=_RAW_SN123)
    # 先置为未验证，recheck 后应变回 verified
    client.patch(f"/api/disks/{DISK}", json={"identity_verified": False})

    r = client.post(f"/api/disks/{DISK}/identity/recheck")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["verdict"] == "verified"
    assert body["identity_serial"] == "SN123"
    assert body["capacity_bytes"] == 500107862016

    row = _disk_row(client, DISK)
    assert row["identity_verified"] == 1
    assert row["identity_verified_source"] == "auto"
    assert row["identity_verified_at"]
    assert row["identity_status"] == "verified"


def test_recheck_verified_updates_physical_serial(client: TestClient,
                                                  monkeypatch) -> None:
    # 登记的 physical_serial 与现场读到的不同：verified 后顺带更正记录值
    client.patch(f"/api/disks/{DISK}", json={"physical_serial": "BOXID"})
    _Stub(monkeypatch, attached=[_attached_item("SN123")], raw=_RAW_SN123,
          mapped_by="unique_capacity_match")
    r = client.post(f"/api/disks/{DISK}/identity/recheck")
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "verified"
    assert body["identity_serial"] == "SN123"
    row = _disk_row(client, DISK)
    assert row["physical_serial"] == "SN123"  # 顺带更正
    assert row["identity_verified_source"] == "auto"


def test_recheck_unverified(client: TestClient, monkeypatch) -> None:
    _Stub(monkeypatch, attached=[_attached_item("SN123")], raw=_RAW_OTHER)
    r = client.post(f"/api/disks/{DISK}/identity/recheck")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["verdict"] == "unverified"
    assert "OTHERSER" in body["reason"]
    row = _disk_row(client, DISK)
    assert row["identity_verified"] == 0
    assert row["identity_verified_source"] == "auto"


def test_recheck_not_attached(client: TestClient, monkeypatch) -> None:
    _Stub(monkeypatch, attached=[_attached_item("SOMEOTHER")])
    r = client.post(f"/api/disks/{DISK}/identity/recheck")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["verdict"] == "not_attached"
    assert body["identity_serial"] is None
    assert body["capacity_bytes"] is None
    # 不在线不改变已验证状态
    assert _disk_row(client, DISK)["identity_verified"] is None


def test_recheck_unknown_disk_404(client: TestClient, monkeypatch) -> None:
    _Stub(monkeypatch, attached=[])
    assert client.post("/api/disks/NOPE/identity/recheck").status_code == 404
