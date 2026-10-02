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
    monkeypatch.setattr(
        smart, "read_smart_verbose",
        lambda dev=None, *, devices=None, **kw: _ok_verbose())
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

    def fail(dev=None, *, devices=None, **kw) -> dict:
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


def test_smart_read_live_uses_candidate_chain(client: TestClient,
                                              monkeypatch) -> None:
    """现场读取走候选链：attached 项的设备传入 read_smart_verbose(devices=...)。"""
    seen: dict = {}

    def fake_cands(disk_index, letter=None):
        seen["disk_index"] = disk_index
        return [{"device": "/dev/sdb", "type": "nvme", "source": "scan"},
                {"device": "\\\\.\\PhysicalDrive1", "type": "", "source": "fallback-pd"}]

    monkeypatch.setattr(smart, "smart_device_candidates", fake_cands)

    def fake_verbose(dev=None, *, devices=None, **kw):
        seen["devices"] = devices
        return {"ok": True, "raw": SAMPLE, "device": "/dev/sdb",
                "device_type": "nvme", "exit_status": 0, "reason": None,
                "message": None, "raw_excerpt": "", "attempts": [],
                "device_candidates": [d["device"] for d in (devices or [])],
                "scan_info": {"candidates": devices or []}}

    monkeypatch.setattr(smart, "read_smart_verbose", fake_verbose)
    monkeypatch.setattr(routes_disks, "_attached_linux", lambda: {
        "available": True,
        "items": [{"device": "/dev/sdb", "model": "M", "serial": "D1",
                   "size_bytes": 100, "volumes": []}],
        "count": 1,
    })
    # 平台是 linux → _disk_index_of 认不出 PhysicalDrive → 走 attached 单候选
    r = client.post("/api/disks/D1/smart/read")
    assert r.status_code == 200
    body = r.json()
    assert seen["devices"][0]["device"] == "/dev/sdb"
    assert body["device_candidates"] == ["/dev/sdb"]
    assert seen.get("disk_index") is None  # linux 设备名不产盘号，不走 smart_device_candidates


def test_smart_read_live_physicaldrive_index_chain(client: TestClient,
                                                   monkeypatch) -> None:
    """attached 项是 \\\\.\\PhysicalDriveN 时提取盘号 → smart_device_candidates(N)。"""
    seen: dict = {}

    def fake_cands(disk_index, letter=None):
        seen["disk_index"] = disk_index
        return [{"device": "/dev/sdb", "type": "", "source": "fallback-sd"},
                {"device": f"\\\\.\\PhysicalDrive{disk_index}", "type": "",
                 "source": "fallback-pd"}]

    monkeypatch.setattr(smart, "smart_device_candidates", fake_cands)
    monkeypatch.setattr(
        smart, "read_smart_verbose",
        lambda dev=None, *, devices=None, **kw: {
            "ok": False, "raw": None, "device": "", "device_type": "",
            "exit_status": None, "reason": "device_open",
            "message": smart.REASON_MESSAGES["device_open"],
            "raw_excerpt": "", "attempts": [],
            "device_candidates": [d["device"] for d in (devices or [])],
            "scan_info": {}})
    monkeypatch.setattr(routes_disks, "_attached_linux", lambda: {
        "available": True,
        "items": [{"device": "\\\\.\\PhysicalDrive1", "model": "M",
                   "serial": "D1", "size_bytes": None, "volumes": []}],
        "count": 1,
    })
    r = client.post("/api/disks/D1/smart/read")
    assert r.status_code == 200
    assert seen["disk_index"] == 1
    body = r.json()
    assert body["device_candidates"] == ["/dev/sdb", "\\\\.\\PhysicalDrive1"]
    assert body["reason"] == "device_open"


def test_capacity_screen_mismatch(client: TestClient, monkeypatch) -> None:
    """容量软校验：scan 候选容量与已知容量差 >20% → 剔除并标 mismatch。"""
    cands = [{"device": "/dev/sda", "type": "", "source": "scan"},
             {"device": "/dev/sdb", "type": "", "source": "fallback-sd"}]
    monkeypatch.setattr(smart, "device_capacity", lambda dev: 500_000_000_000)
    out, status = routes_disks._capacity_screen(cands, 1_000_000_000_000)
    assert [c["device"] for c in out] == ["/dev/sdb"]
    assert status == "mismatch"
    # 容量接近 → 保留
    out, status = routes_disks._capacity_screen(cands, 520_000_000_000)
    assert [c["device"] for c in out] == ["/dev/sda", "/dev/sdb"]
    assert status is None
    # 任一方无容量 → 不校验
    out, status = routes_disks._capacity_screen(cands, None)
    assert out == cands and status is None
    out, status = routes_disks._capacity_screen(cands, 100)
    monkeypatch.setattr(smart, "device_capacity", lambda dev: None)
    out, status = routes_disks._capacity_screen(cands, 100)
    assert out == cands and status is None


# ---------------------------------------------------------------- preflight


def test_preflight_ok(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "disk"
    target.mkdir()
    monkeypatch.setattr(smart, "check_smartctl", lambda: "/usr/bin/smartctl")
    monkeypatch.setattr(smart, "device_for_path", lambda p: "/dev/sdz")
    monkeypatch.setattr(
        smart, "read_smart_verbose",
        lambda dev=None, *, devices=None, **kw: {
            "ok": True, "raw": SAMPLE, "device": "/dev/sdz",
            "device_type": "sat", "exit_status": 0, "reason": None,
            "message": None, "raw_excerpt": "", "attempts": []})
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
    monkeypatch.setattr(
        smart, "read_smart_verbose",
        lambda dev=None, *, devices=None, **kw: {
            "ok": False, "raw": None, "device_type": "", "exit_status": None,
            "reason": "device_open", "message": "打不开",
            "raw_excerpt": "", "attempts": []})
    body = client.post("/api/collect/preflight", json={"path": str(target)}).json()
    assert body["is_smart_capable"] is False
    assert any("SMART" in w for w in body["warnings"])


# ---------------------------------------------------------------- SSD 契约字段


def _mk_nvme_disk(tmp_cat) -> None:
    """在 catalog 里加一块 NVMe 盘 D2 + 卷 + 快照，并落 NVMe SMART 行。"""
    from test_smart import NVME_SAMPLE  # noqa: E402

    cat = tmp_cat
    ensure_disk(cat, "D2", physical_model="WD Blue SN570 2TB SSD")
    ensure_volume(cat, "vol2", "D2", filesystem="ntfs")
    register_snapshot(cat, "vol2/20260302T000000Z", "vol2", status="sealed",
                      host_path="/y", collected_at="2026-03-02T00:00:00Z")
    smart.record_smart(cat, "D2", "vol2/20260302T000000Z",
                       smart.parse_smart(NVME_SAMPLE),
                       collected_at="2026-03-02T00:00:00Z", raw_json=NVME_SAMPLE)
    cat.commit()


@pytest.fixture()
def client_with_nvme(tmp_path: Path) -> TestClient:
    data_root = tmp_path / "data"
    data_root.mkdir()
    _build_snapshot_db(data_root / "vol" / "20260101T000000Z" / "snapshot.db", SAMPLE)
    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    ensure_disk(cat, "D1", physical_model="TOSHIBA HDWG480")
    ensure_volume(cat, "vol", "D1", filesystem="ext4")
    register_snapshot(cat, SID_OLD, "vol", status="sealed", host_path="/x",
                      collected_at="2026-01-01T00:00:00Z")
    _mk_nvme_disk(cat)
    cat.close()
    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c


def test_disk_detail_ssd_contract(client_with_nvme: TestClient) -> None:
    r = client_with_nvme.get("/api/disks/D2")
    assert r.status_code == 200
    body = r.json()
    ssd = body["ssd"]
    assert set(ssd.keys()) == set(smart.SSD_CONTRACT_KEYS)
    assert ssd["life_left_pct"] == 100
    assert ssd["percentage_used"] == 0
    assert ssd["written_bytes"] == 17379040 * 1000 * 512
    assert abs(ssd["written_tb"] - 8.9) < 0.1
    assert ssd["temp_sensors"] == [82, 52]
    assert ssd["source"] == "nvme"
    # 集成回归：前端健康卡读的是 latest_smart.ssd（与 /smart 历史项形状一致），
    # 两层必须是同一份契约 —— 曾经只有顶层有、内层为 null（SSD 区块显示"未采集到"）。
    assert body["latest_smart"]["ssd"] == ssd
    # HDD 盘 → 全 null / [] 兜底
    d1 = client_with_nvme.get("/api/disks/D1").json()
    assert d1["latest_smart"]["health"] == "passed"
    assert d1["ssd"]["life_left_pct"] is None
    assert d1["ssd"]["temp_sensors"] == []
    assert d1["latest_smart"]["ssd"] == d1["ssd"]


def test_smart_history_ssd_fields(client_with_nvme: TestClient) -> None:
    r = client_with_nvme.get("/api/disks/D2/smart")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    ssd = items[0]["ssd"]
    assert set(ssd.keys()) == set(smart.SSD_CONTRACT_KEYS)
    assert ssd["source"] == "nvme"
    assert ssd["unsafe_shutdowns"] == 22
    assert ssd["power_cycles"] == 308
    assert ssd["controller_busy_minutes"] == 174
    assert ssd["read_bytes"] == 15578192 * 1000 * 512


def test_smart_history_meta_backfill_ssd_contract(client_with_nvme: TestClient) -> None:
    """meta 回填项（catalog 无行）也带 SSD 契约字段（HDD → 全 null 兜底）。"""
    r = client_with_nvme.get("/api/disks/D1/smart")
    items = r.json()["items"]
    assert items[0]["source"] == "meta"
    assert set(items[0]["ssd"].keys()) == set(smart.SSD_CONTRACT_KEYS)
    assert items[0]["ssd"]["source"] is None
    assert items[0]["ssd"]["temp_sensors"] == []


def test_smart_read_response_ssd(client_with_nvme: TestClient, monkeypatch) -> None:
    from test_smart import NVME_SAMPLE  # noqa: E402

    monkeypatch.setattr(routes_disks, "_attached_linux", lambda: {
        "available": True,
        "items": [{"device": "/dev/sdb", "model": "M", "serial": "D2",
                   "size_bytes": 100, "volumes": []}],
        "count": 1,
    })
    monkeypatch.setattr(
        smart, "read_smart_verbose",
        lambda dev=None, *, devices=None, **kw:
            _ok_verbose(NVME_SAMPLE, device_type="nvme"))
    body = client_with_nvme.post("/api/disks/D2/smart/read").json()
    assert body["ok"] is True
    ssd = body["ssd"]
    assert set(ssd.keys()) == set(smart.SSD_CONTRACT_KEYS)
    assert ssd["life_left_pct"] == 100 and ssd["source"] == "nvme"

    # 读取失败 → ssd 为 null
    monkeypatch.setattr(smart, "read_smart_verbose",
                        lambda dev=None, *, devices=None, **kw: {
                            "ok": False, "raw": None, "device": "",
                            "device_type": "", "exit_status": None,
                            "reason": "timeout", "message": "超时",
                            "raw_excerpt": "", "attempts": []})
    body = client_with_nvme.post("/api/disks/D2/smart/read").json()
    assert body["ok"] is False and body["ssd"] is None


# ---------------------------------------------------------------- ATA 属性表契约


@pytest.fixture()
def ata_client(tmp_path: Path) -> TestClient:
    """D1 的最新 SMART 为完整 ATA 盘（关键指标/属性表齐全）。"""
    from test_smart import ATA_FULL

    data_root = tmp_path / "data"
    data_root.mkdir()
    _build_snapshot_db(data_root / "vol" / "20260301T000000Z" / "snapshot.db", None)

    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    ensure_disk(cat, "D1", physical_model="TOSHIBA MQ04UBB400", capacity_bytes=4000)
    ensure_volume(cat, "vol", "D1", filesystem="ext4")
    register_snapshot(cat, SID_NEW, "vol", status="sealed", host_path="/x",
                      collected_at="2026-03-01T00:00:00Z")
    smart.record_smart(cat, "D1", SID_NEW, smart.parse_smart(ATA_FULL),
                       collected_at="2026-03-01T00:00:00Z", raw_json=ATA_FULL)
    cat.commit()
    cat.close()
    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c


_ATA_FLAT_KEYS = ("power_cycle_count", "load_cycle_count", "udma_crc_errors",
                  "raw_read_error_rate", "seek_error_rate", "spin_retry_count",
                  "power_off_retract_count", "airflow_temperature_c",
                  "head_flying_hours", "interface_speed_current",
                  "interface_speed_max", "sata_version", "ata_version",
                  "trim", "zoned", "model_family")


def test_disk_detail_ata_contract(ata_client: TestClient) -> None:
    """详情端点：顶层与 latest_smart 内层两层一致。"""
    d = ata_client.get("/api/disks/D1").json()
    ls = d["latest_smart"]
    assert len(ls["ata_attributes"]) == 15
    assert ls["power_cycle_count"] == 4627
    assert ls["udma_crc_errors"] == 0
    assert ls["sata_version"] == "SATA 3.3"
    assert ls["trim"] is False
    for k in _ATA_FLAT_KEYS:
        assert d[k] == ls[k], k
    assert len(d["ata_attributes"]) == 15


def test_disk_detail_ata_null_fallback(client: TestClient) -> None:
    """非 ATA 盘（NVMe/旧数据）：ata_attributes == []、扁平键 null/false 兜底。"""
    d = client.get("/api/disks/D1").json()
    ls = d["latest_smart"]
    # SAMPLE 的 ATA 表只有 5 项（无关键指标属性）→ 扁平键全 null 兜底
    assert isinstance(ls["ata_attributes"], list)
    assert ls["trim"] is False
    for k in _ATA_FLAT_KEYS:
        if k not in ("trim", "model_family"):  # SAMPLE 带 model_family
            assert ls[k] is None, k


def test_smart_history_ata_contract(ata_client: TestClient) -> None:
    body = ata_client.get("/api/disks/D1/smart").json()
    items = body["items"]
    assert len(items) == 1  # ata_client 只注册了一个快照（无 meta 回填项）
    last = items[0]
    assert len(last["ata_attributes"]) == 15
    assert last["power_cycle_count"] == 4627
    assert last["head_flying_hours"] == 1157774408


def test_smart_read_ata_contract(ata_client: TestClient, monkeypatch) -> None:
    from test_smart import ATA_FULL

    monkeypatch.setattr(routes_disks, "_attached_linux", lambda: {
        "available": True,
        "items": [{"device": "/dev/sdb", "model": "M", "serial": "D1",
                   "size_bytes": 100, "volumes": []}],
        "count": 1,
    })
    monkeypatch.setattr(
        smart, "read_smart_verbose",
        lambda dev=None, *, devices=None, **kw: {
            "ok": True, "raw": ATA_FULL, "device": "/dev/sdb",
            "device_candidates": ["/dev/sdb"], "device_type": "sat",
            "exit_status": 0, "reason": None, "message": None,
            "raw_excerpt": ATA_FULL[:512], "attempts": [],
            "scan_info": {}})
    body = ata_client.post("/api/disks/D1/smart/read").json()
    assert body["ok"] is True
    parsed = body["parsed"]
    assert len(parsed["ata_attributes"]) == 15
    assert parsed["power_cycle_count"] == 4627
    assert parsed["sata_version"] == "SATA 3.3"
