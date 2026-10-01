"""磁盘 API（P4-②）测试：列表/详情/历史/attached/preflight。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest import smart
from cold_manifest.api import routes_disks
from cold_manifest.catalog import catalog_path, ensure_disk, ensure_volume, register_snapshot
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.server import create_app

from test_smart import SAMPLE  # noqa: E402 — pytest rootdir 插入 tests 目录

SID_OLD = "vol/20260101T000000Z"  # meta 有 smart_raw_json、历史表无行（读时回填）
SID_NEW = "vol/20260301T000000Z"  # disk_smart 有 catalog 行


def _build_snapshot_db(path: Path, smart_raw: "str | None",
                       smart_error: "str | None" = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    init_snapshot(conn)
    conn.execute("INSERT INTO entries(entry_id, parent_id, path, name, depth, type,"
                 " size_bytes) VALUES(1, 0, '.', '', 0, 'dir', NULL)")
    if smart_raw is not None:
        conn.execute("INSERT INTO meta(key, value) VALUES('smart_raw_json', ?)",
                     (smart_raw,))
    if smart_error is not None:
        conn.execute("INSERT INTO meta(key, value) VALUES('smart_error', ?)",
                     (smart_error,))
    conn.commit()
    conn.close()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _build_snapshot_db(data_root / "vol" / "20260101T000000Z" / "snapshot.db", SAMPLE)
    _build_snapshot_db(data_root / "vol" / "20260301T000000Z" / "snapshot.db", None,
                       smart_error="权限不足：读取 SMART 需要管理员权限")

    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    ensure_disk(cat, "D1", physical_model="TOSHIBA HDWG480", capacity_bytes=8000)
    ensure_volume(cat, "vol", "D1", filesystem="ext4")
    register_snapshot(cat, SID_OLD, "vol", status="sealed", host_path="/x",
                      collected_at="2026-01-01T00:00:00Z")
    register_snapshot(cat, SID_NEW, "vol", status="sealed", host_path="/x",
                      collected_at="2026-03-01T00:00:00Z")
    smart.record_smart(cat, "D1", SID_NEW, smart.parse_smart(SAMPLE),
                       collected_at="2026-03-01T00:00:00Z", raw_json=SAMPLE)
    cat.commit()
    cat.close()

    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c


# ---------------------------------------------------------------- 列表 / 详情


def test_list_disks(client: TestClient) -> None:
    r = client.get("/api/disks")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    d = body["items"][0]
    assert d["disk_id"] == "D1"
    assert d["volume_count"] == 1 and d["snapshot_count"] == 2
    assert d["latest_smart"]["health"] == "passed"
    assert d["latest_smart"]["temperature_c"] == 31
    assert d["latest_smart"]["collected_at"] == "2026-03-01T00:00:00Z"


def test_disk_detail(client: TestClient) -> None:
    r = client.get("/api/disks/D1")
    assert r.status_code == 200
    d = r.json()
    assert [v["volume_id"] for v in d["volumes"]] == ["vol"]
    assert [s["snapshot_id"] for s in d["snapshots"]] == [SID_OLD, SID_NEW]
    assert d["latest_smart"]["health"] == "passed"

    assert client.get("/api/disks/NOPE").status_code == 404


# ---------------------------------------------------------------- SMART 历史


def test_smart_history_with_meta_backfill(client: TestClient) -> None:
    r = client.get("/api/disks/D1/smart")
    assert r.status_code == 200
    items = r.json()["items"]
    # SID_OLD 走 meta 现场解析（不写库）；SID_NEW 有 catalog 行；按 collected_at 升序
    assert [i["snapshot_id"] for i in items] == [SID_OLD, SID_NEW]
    assert items[1]["source"] == "catalog"
    assert items[0]["source"] == "meta"
    assert items[0]["health"] == "passed"
    assert items[0]["temperature_c"] == 31
    # 回填不落库
    assert client.get("/api/disks/D1/smart").json()["items"][0]["source"] == "meta"


def test_smart_history_404(client: TestClient) -> None:
    assert client.get("/api/disks/NOPE/smart").status_code == 404


# ---------------------------------------------------------------- attached


def test_attached_ok(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(routes_disks, "_attached_linux", lambda: {
        "available": True,
        "items": [{"device": "/dev/sdb", "model": "M", "serial": "S",
                   "size_bytes": 100, "volumes": [{"path": "/mnt/x"}]}],
        "count": 1,
    })
    body = client.get("/api/disks/attached").json()
    assert body["available"] is True and body["count"] == 1


def test_attached_degrades(client: TestClient, monkeypatch) -> None:
    def boom():
        raise RuntimeError("lsblk 不可用")

    monkeypatch.setattr(routes_disks, "_attached_linux", boom)
    body = client.get("/api/disks/attached").json()
    assert body["available"] is False
    assert "lsblk" in body["reason"]


# ---------------------------------------------------------------- smart_error 透出


def test_detail_and_history_expose_smart_error(client: TestClient) -> None:
    detail = client.get("/api/disks/D1").json()
    assert detail["smart_error"]["snapshot_id"] == SID_NEW
    assert "管理员" in detail["smart_error"]["smart_error"]

    hist = client.get("/api/disks/D1/smart").json()
    assert hist["smart_error"]["snapshot_id"] == SID_NEW


# ---------------------------------------------------------------- 现场读取


def _ok_verbose(raw: str = SAMPLE, device_type: str = "sat") -> dict:
    return {"ok": True, "raw": raw, "device_type": device_type, "reason": None,
            "message": None, "raw_excerpt": raw[:2048], "attempts": []}


def test_smart_read_live_ok(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(routes_disks, "_attached_linux", lambda: {
        "available": True,
        "items": [{"device": "/dev/sdb", "model": "M", "serial": "D1",
                   "size_bytes": 100, "volumes": []}],
        "count": 1,
    })
    monkeypatch.setattr(smart, "read_smart_verbose", lambda dev: _ok_verbose())
    r = client.post("/api/disks/D1/smart/read")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["device"] == "/dev/sdb"
    assert body["device_type"] == "sat"
    assert body["parsed"]["health"] == "passed"
    assert body["reason"] is None


def test_smart_read_live_failure_readable(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(routes_disks, "_attached_linux", lambda: {
        "available": True,
        "items": [{"device": "/dev/sdb", "model": "M", "serial": "D1",
                   "size_bytes": 100, "volumes": []}],
        "count": 1,
    })

    def fail(dev: str) -> dict:
        return {"ok": False, "raw": None, "device_type": "",
                "reason": "permission_denied",
                "message": smart.REASON_MESSAGES["permission_denied"],
                "raw_excerpt": "Access is denied",
                "attempts": [{"device_type": "default", "rc": 16, "error": None,
                              "stderr_excerpt": "Access is denied"}]}

    monkeypatch.setattr(smart, "read_smart_verbose", fail)
    r = client.post("/api/disks/D1/smart/read")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "permission_denied"
    assert "管理员" in body["message"]
    assert body["parsed"] is None
    assert body["raw_excerpt"] == "Access is denied"


def test_smart_read_live_disk_absent_404(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(routes_disks, "_attached_linux", lambda: {
        "available": True, "items": [], "count": 0})
    r = client.post("/api/disks/NOPE/smart/read")
    assert r.status_code == 404
    assert "不在线" in r.json()["detail"]


# ---------------------------------------------------------------- preflight


def test_preflight_ok(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "disk"
    target.mkdir()
    monkeypatch.setattr(smart, "check_smartctl", lambda: "/usr/bin/smartctl")
    monkeypatch.setattr(smart, "device_for_path", lambda p: "/dev/sdz")
    monkeypatch.setattr(smart, "read_smart",
                        lambda dev: {"raw": SAMPLE, "device_type": "sat"})
    body = client.post("/api/collect/preflight", json={"path": str(target)}).json()
    assert body["writable"] is True
    assert body["smartctl_available"] is True
    assert body["smartctl_path"] == "/usr/bin/smartctl"
    assert body["device_type_hint"] == "sat"
    assert body["is_smart_capable"] is True
    assert body["warnings"] == []


def test_preflight_no_smartctl(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "disk"
    target.mkdir()
    monkeypatch.setattr(smart, "check_smartctl", lambda: None)
    body = client.post("/api/collect/preflight", json={"path": str(target)}).json()
    assert body["writable"] is True
    assert body["smartctl_available"] is False
    assert body["is_smart_capable"] is False
    assert any("smartctl" in w for w in body["warnings"])


def test_preflight_missing_path(client: TestClient, tmp_path: Path) -> None:
    body = client.post("/api/collect/preflight",
                       json={"path": str(tmp_path / "nope")}).json()
    assert body["writable"] is False
    assert any("不存在" in w for w in body["warnings"])


def test_preflight_sm_failures_degrade(client: TestClient, tmp_path: Path,
                                       monkeypatch) -> None:
    target = tmp_path / "disk"
    target.mkdir()
    monkeypatch.setattr(smart, "check_smartctl", lambda: "/usr/bin/smartctl")

    def boom(p):
        raise RuntimeError("no mount")

    monkeypatch.setattr(smart, "device_for_path", boom)
    body = client.post("/api/collect/preflight", json={"path": str(target)}).json()
    assert body["is_smart_capable"] is False
    assert any("物理盘" in w for w in body["warnings"])

    monkeypatch.setattr(smart, "device_for_path", lambda p: "/dev/sdz")
    monkeypatch.setattr(smart, "read_smart", lambda dev: None)
    body = client.post("/api/collect/preflight", json={"path": str(target)}).json()
    assert body["is_smart_capable"] is False
    assert any("SMART" in w for w in body["warnings"])
