"""磁盘身份去歧义：probe 撞车（USB 盒两块盘报相同序列号）时按物理序列号分盘分卷。

- 两块盘 probe 序列号相同、physical_serial 不同 → 各自 disk/volume 行；
- 同一块盘再采 → 复用同一 disk_id/volume_id（含 probe 值风格的历史卷名）；
- 探针与物理序列号都不可用 → VOL-… 回退 + warning；
- --serial / --volume-id 显式覆盖仍然最高优先。
"""

import sqlite3
import time
from pathlib import Path

import pytest

from cold_manifest.catalog import connect_catalog
from cold_manifest.collect import collect_volume
from cold_manifest.probe import DiskInfo, VolumeInfo


def _make_probe(*, probe_serial: str, physical_serial: str,
                model: str = "P300 4TB", mount: str = "/mnt/fake"):
    vol = VolumeInfo(
        filesystem="ntfs", label="FAKE", volume_serial_hex="ABCD-1234",
        partition_uuid="1111-2222", partition_index=2, partition_table_type="GPT",
        capacity_bytes=4_000_000_000, free_bytes=3_000_000_000,
        mount_point=mount, device_path="/dev/sdb2",
    )
    disk = DiskInfo(
        physical_model=model, physical_serial=physical_serial,
        disk_serial=probe_serial, serial_source="probe",
        bridge_model="USB Bridge", interface_type="USB",
        capacity_bytes=4_000_000_000, firmware="fw1",
        smart_status="unavailable",
    )

    def probe(path, *, manual_serial=None, smartctl=True):
        if manual_serial is not None:
            disk.disk_serial = manual_serial
            disk.serial_source = "manual"
        return vol, disk

    return probe


def _make_tree(root: Path) -> None:
    (root / "d").mkdir(parents=True)
    (root / "d" / "a.txt").write_bytes(b"x" * 10)


def _cat(data_root: Path) -> sqlite3.Connection:
    return connect_catalog(data_root)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    data_root = tmp_path / "data"
    state = {}

    def collect(*, probe_serial: str, physical_serial: str, model: str = "P300 4TB",
                **kwargs):
        p = _make_probe(probe_serial=probe_serial, physical_serial=physical_serial,
                        model=model, mount=str(scan_root))
        monkeypatch.setattr("cold_manifest.collect.probe_path", p)
        return collect_volume(scan_root, data_root=data_root, **kwargs)

    state["collect"] = collect
    state["data_root"] = data_root
    return state


def test_two_disks_same_probe_serial_split_by_physical(env) -> None:
    """probe 撞车 + 物理序列号不同 → 两行磁盘、两卷，卷名以各自物理序列号开头。"""
    r1 = env["collect"](probe_serial="20260123004775F", physical_serial="X0FGBM2AS")
    r2 = env["collect"](probe_serial="20260123004775F", physical_serial="X0DG6A2GS")
    assert r1.volume_id == "X0FGBM2AS_P2"
    assert r2.volume_id == "X0DG6A2GS_P2"
    cat = _cat(env["data_root"])
    try:
        disks = {r["disk_id"]: r for r in cat.execute("SELECT * FROM disks")}
        assert set(disks) == {"X0FGBM2AS", "X0DG6A2GS"}
        vols = {r["volume_id"]: r for r in cat.execute("SELECT * FROM volumes")}
        assert set(vols) == {"X0FGBM2AS_P2", "X0DG6A2GS_P2"}
        assert vols["X0FGBM2AS_P2"]["disk_id"] == "X0FGBM2AS"
        assert vols["X0DG6A2GS_P2"]["disk_id"] == "X0DG6A2GS"
        # 第一块盘的行未被第二块覆盖
        assert disks["X0FGBM2AS"]["physical_serial"] == "X0FGBM2AS"
        assert disks["X0FGBM2AS"]["physical_model"] == "P300 4TB"
    finally:
        cat.close()


def test_same_disk_recollected_reuses_ids(env) -> None:
    """同一块盘再采 → 复用同一 disk_id/volume_id（历史不分裂）。"""
    r1 = env["collect"](probe_serial="SER1", physical_serial="PHY1")
    time.sleep(1.1)  # 避免同秒目标目录冲突
    r2 = env["collect"](probe_serial="SER1", physical_serial="PHY1")
    assert r1.volume_id == r2.volume_id == "PHY1_P2"
    assert r1.snapshot_id != r2.snapshot_id
    cat = _cat(env["data_root"])
    try:
        assert cat.execute("SELECT COUNT(*) FROM disks").fetchone()[0] == 1
        assert cat.execute("SELECT COUNT(*) FROM volumes").fetchone()[0] == 1
    finally:
        cat.close()


def test_probe_style_history_volume_reused(env) -> None:
    """历史卷名是 probe 值风格（旧版登记，无物理序列号）→ 同盘再采仍复用原名。"""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cat = _cat(env["data_root"])
    try:
        cat.execute(
            "INSERT INTO disks(disk_id, physical_model, capacity_bytes,"
            " first_seen, last_seen) VALUES('20260123004775F','P300 4TB',"
            " 4000000000, ?, ?)", (now, now))
        cat.execute(
            "INSERT INTO volumes(volume_id, disk_id, partition_index)"
            " VALUES('20260123004775F_P2', '20260123004775F', 2)")
        cat.commit()
    finally:
        cat.close()

    r = env["collect"](probe_serial="20260123004775F", physical_serial="16NDT0O1T")
    assert r.volume_id == "20260123004775F_P2"
    cat = _cat(env["data_root"])
    try:
        assert cat.execute("SELECT COUNT(*) FROM disks").fetchone()[0] == 1
        assert cat.execute("SELECT COUNT(*) FROM volumes").fetchone()[0] == 1
        # 物理序列号回填到既有盘行
        row = cat.execute("SELECT physical_serial FROM disks").fetchone()
        assert row["physical_serial"] == "16NDT0O1T"
    finally:
        cat.close()


def test_unusable_serials_fall_back_to_volume_hex(env, monkeypatch) -> None:
    """探针与物理序列号都不可用 → 卷序列号 hex 回退 + warning（行为不变）。"""
    r = env["collect"](probe_serial="0", physical_serial="")
    assert r.volume_id == "VOL-ABCD-1234_P2"
    assert any("回退用卷序列号" in w for w in r.warnings)


@pytest.mark.parametrize("placeholder", [
    "0", "0000000",
    "0123456789ABCDEF", "0123456789abcdef", "0123456789AbCdEf",
    "XXXXXXXX", "AAAAAAAA",
])
def test_placeholder_serials_not_used_as_identity(env, placeholder) -> None:
    """已知占位形态（JMicron 盒报 0123456789ABCDEF 等）→ 不可用作盘身份，
    回退 VOL-… 命名 + warning；正常序列号不受影响。"""
    from cold_manifest.catalog import _usable_serial

    assert not _usable_serial(placeholder)
    r = env["collect"](probe_serial=placeholder, physical_serial="")
    assert r.volume_id == "VOL-ABCD-1234_P2"
    assert any("回退用卷序列号" in w for w in r.warnings)
    assert _usable_serial("SERFAKE123")
    assert _usable_serial("X0FGBM2AS")


def test_explicit_overrides_win(env) -> None:
    """--volume-id / --serial 显式覆盖仍然最高优先。"""
    r = env["collect"](probe_serial="SERX", physical_serial="PHYX",
                       volume_id="MYVOL")
    assert r.volume_id == "MYVOL"
    time.sleep(1.1)
    r2 = env["collect"](probe_serial="SERY", physical_serial="PHYX",
                        manual_serial="MYMANUAL")
    assert r2.volume_id == "MYMANUAL_P2"
