"""PR-A：磁盘身份冲突检测（ensure_disk / _register_catalog）。"""

import json

import pytest

from cold_manifest.catalog import (
    _detect_identity_conflicts,
    connect_catalog,
    ensure_disk,
)
from cold_manifest.collect import _register_catalog
from cold_manifest.probe import DiskInfo, VolumeInfo


BASE = dict(
    physical_model="WD Elements 25A3",
    physical_serial="WX12ABCD",
    bridge_model="USB Bridge",
    capacity_bytes=1_000_000_000_000,
    interface_type="USB",
)


def _row(cat, disk_id):
    return cat.execute("SELECT * FROM disks WHERE disk_id=?", (disk_id,)).fetchone()


@pytest.fixture()
def cat(tmp_path):
    conn = connect_catalog(tmp_path / "data")
    yield conn
    conn.close()


def test_capacity_conflict(cat):
    ensure_disk(cat, "SER1", **BASE)
    res = ensure_disk(cat, "SER1", physical_model=BASE["physical_model"],
                      physical_serial=BASE["physical_serial"],
                      capacity_bytes=800_000_000_000)
    assert not res["inserted"]
    assert [c["field"] for c in res["conflicts"]] == ["capacity_bytes"]
    c = res["conflicts"][0]
    assert c["existing"] == 1_000_000_000_000
    assert c["incoming"] == 800_000_000_000
    row = _row(cat, "SER1")
    assert row["identity_verified"] == 0
    hist = json.loads(row["identity_conflict_json"])
    assert len(hist) == 1 and hist[0]["conflicts"] == res["conflicts"]
    # 既有字段未被覆盖
    assert row["capacity_bytes"] == 1_000_000_000_000


def test_model_conflict(cat):
    ensure_disk(cat, "SER2", **BASE)
    res = ensure_disk(cat, "SER2", physical_model="Seagate Expansion",
                      physical_serial=BASE["physical_serial"],
                      capacity_bytes=BASE["capacity_bytes"])
    assert [c["field"] for c in res["conflicts"]] == ["physical_model"]
    row = _row(cat, "SER2")
    assert row["identity_verified"] == 0
    assert row["physical_model"] == "WD Elements 25A3"  # 未覆盖
    assert "physical_model" in row["identity_conflict_json"]


def test_serial_conflict(cat):
    ensure_disk(cat, "SER3", **BASE)
    res = ensure_disk(cat, "SER3", physical_model=BASE["physical_model"],
                      physical_serial="OTHERSERIAL",
                      capacity_bytes=BASE["capacity_bytes"])
    assert [c["field"] for c in res["conflicts"]] == ["physical_serial"]
    assert res["conflicts"][0]["existing"] == "WX12ABCD"
    row = _row(cat, "SER3")
    assert row["identity_verified"] == 0
    assert row["physical_serial"] == "WX12ABCD"  # 未覆盖


def test_no_conflict_behavior_unchanged(cat):
    res = ensure_disk(cat, "SER4", **BASE)
    assert res == {"inserted": True, "conflicts": []}
    res2 = ensure_disk(cat, "SER4", physical_model=BASE["physical_model"],
                       physical_serial=BASE["physical_serial"],
                       capacity_bytes=BASE["capacity_bytes"],
                       bridge_model="Other Bridge")  # bridge_model 被 COALESCE 保留首值
    assert res2 == {"inserted": False, "conflicts": []}
    row = _row(cat, "SER4")
    assert row["identity_verified"] is None  # 未传 → 不动
    assert row["identity_conflict_json"] is None
    # 现有 COALESCE 口径不变：非空新值覆盖旧值（bridge_model），空值回填保留
    assert row["bridge_model"] == "Other Bridge"
    ensure_disk(cat, "SER4", physical_model=BASE["physical_model"],
                bridge_model=None)  # None 回填：旧值保留
    assert _row(cat, "SER4")["bridge_model"] == "Other Bridge"
    # caller 显式传 identity_verified 可写/更新
    ensure_disk(cat, "SER4", identity_verified=1,
                physical_model=BASE["physical_model"])
    assert _row(cat, "SER4")["identity_verified"] == 1


def test_conflict_history_capped_at_20(cat):
    ensure_disk(cat, "SER5", **BASE)
    for i in range(25):
        ensure_disk(cat, "SER5", physical_model=BASE["physical_model"],
                    physical_serial=f"S{i}",
                    capacity_bytes=BASE["capacity_bytes"])
    row = _row(cat, "SER5")
    hist = json.loads(row["identity_conflict_json"])
    assert len(hist) == 20
    assert hist[-1]["conflicts"][0]["incoming"] == "S24"


def test_detect_no_conflict_on_unusable_serial(cat):
    # 不可用序列号（空/占位）不参与冲突判定
    ensure_disk(cat, "SER6", physical_model="M1", physical_serial="",
                capacity_bytes=None)
    existing = _row(cat, "SER6")
    assert _detect_identity_conflicts(existing, {
        "physical_model": "M1", "physical_serial": None,
        "capacity_bytes": None}) == []


def test_register_catalog_warns_on_conflict(tmp_path):
    cat = connect_catalog(tmp_path / "data")
    ensure_disk(cat, "WXCLASH", **BASE)
    cat.commit()
    cat.close()

    vol = VolumeInfo(partition_index=1, capacity_bytes=1_000_000_000_000)
    disk = DiskInfo(disk_serial="WXCLASH", physical_model=BASE["physical_model"],
                    physical_serial="TOTALLYDIFFERENT",
                    capacity_bytes=BASE["capacity_bytes"])
    warnings: list[str] = []
    _register_catalog(tmp_path / "data", "volX/20260101T000000Z", "volX",
                      vol, disk, {"file_count": 0, "dir_count": 0,
                                  "total_bytes": 0, "total_alloc": 0,
                                  "zero_byte_count": 0, "max_depth": 0,
                                  "skipped_count": 0},
                      None, False, False, None, None,
                      "2026-01-01T00:00:00Z", warnings,
                      tmp_path / "root")
    assert any(w.startswith("磁盘身份冲突") and "WXCLASH" in w for w in warnings)
    cat = connect_catalog(tmp_path / "data")
    assert cat.execute("SELECT identity_verified FROM disks WHERE disk_id='WXCLASH'"
                       ).fetchone()[0] == 0
    cat.close()
