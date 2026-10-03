"""硬盘盒（USB 桥）采集必须手填序列号：不满足时引擎抛错 / API 400 / CLI rc=2。"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.collect import (
    CollectError,
    ManualSerialRequired,
    collect_volume,
    preflight_serial_required,
    requires_manual_serial,
    serial_required_message,
)
from cold_manifest.probe import DiskInfo, VolumeInfo
from cold_manifest.server import create_app


def _make_probe(*, interface_type="USB", bridge_model="JMicron Generic SCSI Device",
                disk_serial="20260123004775F", volume_serial_hex="",
                physical_serial="", serial_source=None):
    vol = VolumeInfo(
        filesystem="ntfs", label="", volume_serial_hex=volume_serial_hex,
        partition_uuid="", partition_index=2, partition_table_type="MBR",
        capacity_bytes=1_000, free_bytes=500,
        mount_point="/mnt/fake", device_path="/dev/sdb1",
    )
    if serial_source is None:
        serial_source = "probe" if disk_serial else ""
    disk = DiskInfo(
        physical_model="TOSHIBA HDWG480" if physical_serial else "",
        physical_serial=physical_serial, disk_serial=disk_serial,
        serial_source=serial_source,
        bridge_model=bridge_model, interface_type=interface_type,
        capacity_bytes=1_000, firmware="", smart_status="unavailable",
    )

    def probe(path, *, manual_serial=None, smartctl=True):
        if manual_serial is not None:
            disk.disk_serial = manual_serial
            disk.serial_source = "manual"
        return vol, disk

    return probe


def _win(monkeypatch) -> None:
    """让判定走 Windows 口径（测试跑在 Linux 上，monkeypatch 平台探测）。"""
    monkeypatch.setattr("cold_manifest.collect._is_windows", lambda: True)


def _make_tree(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_text("x")


# ---------------------------------------------------------------- 引擎层

def test_usb_with_usable_ata_serial_passes():
    """新规则：USB 盒 + ATA 直通真序列号可用（用户真机形态）→ 放行。"""
    disk = DiskInfo(physical_model="TOSHIBA HDWG480",
                    physical_serial="16NDT0O1T", disk_serial="20260123004775F",
                    bridge_model="Toshiba USB Bridge", interface_type="USB")
    assert serial_required_message(disk, "", None) is None


def test_usb_box_id_usable_passes():
    """新规则：USB 盒 + 只有系统枚举序列号（盒 ID）可用 → 也放行。"""
    disk = DiskInfo(physical_model="", physical_serial="",
                    disk_serial="20260123004775F",
                    bridge_model="JMicron Generic SCSI Device", interface_type="USB")
    assert serial_required_message(disk, "", None) is None


def test_usb_bridge_message_no_serial_at_all():
    disk = DiskInfo(bridge_model="JMicron", interface_type="USB",
                    disk_serial="", physical_serial="")
    msg = serial_required_message(disk, "", None)
    assert "未能读到任何序列号" in msg
    assert "JMicron" in msg  # USB 盒上下文仍写入文案
    assert "--serial" in msg


# ------------------------------------------------- Windows 口径（用户硬规则）

def test_windows_enum_serial_only_blocks(monkeypatch):
    """Windows：只有系统枚举值（Get-Disk/WMI，serial_source=probe）→ 拦，
    即使它看起来"可用"（如 16NDT0O1T）——虚假值不许充当磁盘身份。"""
    _win(monkeypatch)
    disk = DiskInfo(physical_serial="", disk_serial="16NDT0O1T",
                    serial_source="probe",
                    bridge_model="JMicron Generic SCSI Device",
                    interface_type="USB")
    assert requires_manual_serial(disk) is True
    msg = serial_required_message(disk, "", None)
    assert msg is not None
    assert "--serial" in msg
    assert "16NDT0O1T" in msg          # 枚举值仍展示供对照
    assert "盒子/桥的 ID" in msg
    assert "volume-id" in msg          # --volume-id 不放行


def test_windows_box_id_only_blocks(monkeypatch):
    """Windows：真机形态 20260123004775F（盒 ID）→ 拦（Linux 上同夹具放行）。"""
    _win(monkeypatch)
    disk = DiskInfo(disk_serial="20260123004775F", serial_source="probe",
                    bridge_model="JMicron Generic SCSI Device",
                    interface_type="USB")
    assert requires_manual_serial(disk) is True


def test_windows_smartctl_serial_passes(monkeypatch):
    """Windows：serial_source=smartctl（已验证真序列号）→ 放行。"""
    _win(monkeypatch)
    disk = DiskInfo(physical_model="TOSHIBA HDWG480",
                    physical_serial="16NDT0O1T", disk_serial="16NDT0O1T",
                    serial_source="smartctl",
                    bridge_model="Toshiba USB Bridge", interface_type="USB")
    assert requires_manual_serial(disk) is False
    assert serial_required_message(disk, "", None) is None


def test_windows_smartctl_unverified_blocks(monkeypatch):
    """Windows：smartctl 读到序列号但身份未验证（smartctl_unverified）→ 拦，
    可能读到的是别的盘的序列号，宁严勿松。"""
    _win(monkeypatch)
    disk = DiskInfo(physical_serial="SOMEONESERIAL", disk_serial="SOMEONESERIAL",
                    serial_source="smartctl_unverified", interface_type="USB")
    assert requires_manual_serial(disk) is True


def test_windows_manual_serial_passes(monkeypatch):
    """Windows：手填值（serial_source=manual）→ 放行。"""
    _win(monkeypatch)
    disk = DiskInfo(disk_serial="MYDISK123", serial_source="manual",
                    interface_type="USB")
    assert requires_manual_serial(disk) is False
    assert serial_required_message(disk, "", "MYDISK123") is None


def test_windows_volume_id_does_not_bypass(tmp_path, monkeypatch):
    """Windows：--volume-id 只固定卷命名，不替代序列号身份 → 仍拦。"""
    _win(monkeypatch)
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="16NDT0O1T"))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    with pytest.raises(ManualSerialRequired):
        collect_volume(scan_root, data_root=tmp_path / "data",
                       volume_id="MYVOL_P2",
                       on_disk_copy=False, smartctl=False,
                       progress_cb=lambda ph, d, t: None)


def test_windows_collect_enum_serial_only_raises(tmp_path, monkeypatch):
    """Windows：collect_volume 对仅系统枚举值抛 ManualSerialRequired（rc=2/API 400 同源）。"""
    _win(monkeypatch)
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="16NDT0O1T"))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    with pytest.raises(ManualSerialRequired) as ei:
        collect_volume(scan_root, data_root=tmp_path / "data",
                       on_disk_copy=False, smartctl=False,
                       progress_cb=lambda ph, d, t: None)
    assert "16NDT0O1T" in str(ei.value)
    assert "--serial" in str(ei.value)


def test_windows_collect_smartctl_serial_proceeds(tmp_path, monkeypatch):
    """Windows：smartctl 验证过的真序列号 → 采集放行，volume_id 用真序列号。"""
    _win(monkeypatch)
    monkeypatch.setattr(
        "cold_manifest.collect.probe_path",
        _make_probe(disk_serial="16NDT0O1T", physical_serial="16NDT0O1T",
                    serial_source="smartctl"))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    result = collect_volume(scan_root, data_root=tmp_path / "data",
                            on_disk_copy=False, smartctl=False,
                            progress_cb=lambda ph, d, t: None)
    assert result.volume_id == "16NDT0O1T_P2"


def test_windows_collect_manual_serial_proceeds(tmp_path, monkeypatch):
    """Windows：手填序列号 → 采集放行，meta serial_source=manual。"""
    _win(monkeypatch)
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="16NDT0O1T"))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    result = collect_volume(scan_root, data_root=tmp_path / "data",
                            manual_serial="MYDISK123",
                            on_disk_copy=False, smartctl=False,
                            progress_cb=lambda ph, d, t: None)
    assert result.volume_id == "MYDISK123_P2"
    conn = sqlite3.connect(str(result.db_path))
    try:
        assert conn.execute(
            "SELECT value FROM meta WHERE key='serial_source'").fetchone()[0] == "manual"
    finally:
        conn.close()


def test_windows_preflight_matches_collect(monkeypatch):
    """不变量（Windows 口径）：preflight_serial_required 与 collect 同一谓词——
    三条夹具对照：仅系统枚举值=拦 / smartctl=放行 / 手填=放行。"""
    _win(monkeypatch)
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="16NDT0O1T"))
    msg = preflight_serial_required("/mnt/fake", manual_serial=None, smartctl=False)
    assert msg is not None          # 仅系统枚举值 → 拦
    monkeypatch.setattr(
        "cold_manifest.collect.probe_path",
        _make_probe(disk_serial="16NDT0O1T", physical_serial="16NDT0O1T",
                    serial_source="smartctl"))
    assert preflight_serial_required("/mnt/fake", manual_serial=None,
                                     smartctl=False) is None  # smartctl → 放行
    assert preflight_serial_required("/mnt/fake", manual_serial="MYDISK123",
                                     smartctl=False) is None  # 手填 → 放行

def test_usb_bridge_without_serial_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="0", volume_serial_hex=""))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    with pytest.raises(ManualSerialRequired) as ei:
        collect_volume(scan_root, data_root=tmp_path / "data",
                       on_disk_copy=False, smartctl=False,
                       progress_cb=lambda ph, d, t: None)
    msg = str(ei.value)
    assert "JMicron Generic SCSI Device" in msg
    assert "0" in msg  # 盒子报告值写入提示供对照
    assert "--serial" in msg


def test_usb_bridge_with_manual_serial_proceeds(tmp_path, monkeypatch):
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="0", volume_serial_hex=""))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    result = collect_volume(scan_root, data_root=tmp_path / "data",
                            manual_serial="MYDISK123",
                            on_disk_copy=False, smartctl=False,
                            progress_cb=lambda ph, d, t: None)
    assert result.volume_id == "MYDISK123_P2"
    conn = sqlite3.connect(str(result.db_path))
    try:
        assert conn.execute(
            "SELECT value FROM meta WHERE key='serial_source'").fetchone()[0] == "manual"
    finally:
        conn.close()


def test_non_usb_with_usable_probe_serial_ok(tmp_path, monkeypatch):
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(interface_type="SATA", bridge_model="",
                                    disk_serial="SERFAKE123"))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    result = collect_volume(scan_root, data_root=tmp_path / "data",
                            on_disk_copy=False, smartctl=False,
                            progress_cb=lambda ph, d, t: None)
    assert result.volume_id == "SERFAKE123_P2"


@pytest.mark.parametrize("bad", ["", "0", "0123456789ABCDEF"])
def test_non_usb_unusable_probe_serial_requires_manual(tmp_path, monkeypatch, bad):
    # 非 USB 盘但探测序列号占位、ATA 直通也读不到 → 同样要求手填（新规则不区分 USB）
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(interface_type="SATA", bridge_model="",
                                    disk_serial=bad, volume_serial_hex=""))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    with pytest.raises(CollectError) as ei:
        collect_volume(scan_root, data_root=tmp_path / "data",
                       on_disk_copy=False, smartctl=False,
                       progress_cb=lambda ph, d, t: None)
    assert isinstance(ei.value, ManualSerialRequired)
    assert "--serial" in str(ei.value)


def test_volume_id_override_does_not_bypass_serial_requirement(tmp_path, monkeypatch):
    # --volume-id 只固定卷命名，不能替代序列号身份 → 不放行
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="0", volume_serial_hex=""))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    with pytest.raises(ManualSerialRequired):
        collect_volume(scan_root, data_root=tmp_path / "data",
                       volume_id="MYVOL_P2",
                       on_disk_copy=False, smartctl=False,
                       progress_cb=lambda ph, d, t: None)


def test_usb_box_id_with_usable_ata_serial_passes(tmp_path, monkeypatch):
    # 用户真机形态：USB 盒 ID 可见 + ATA 直通真序列号可用 → 放行，
    # volume_id 用真序列号（现有优先级 physical 优先）
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="20260123004775F",
                                    physical_serial="16NDT0O1T"))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    result = collect_volume(scan_root, data_root=tmp_path / "data",
                            on_disk_copy=False, smartctl=False,
                            progress_cb=lambda ph, d, t: None)
    assert result.volume_id == "16NDT0O1T_P2"


def test_usb_box_id_only_also_passes(tmp_path, monkeypatch):
    # 新规则：只有系统枚举序列号（盒 ID）可用、ATA 直通没读到 → 也放行
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="20260123004775F"))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    result = collect_volume(scan_root, data_root=tmp_path / "data",
                            on_disk_copy=False, smartctl=False,
                            progress_cb=lambda ph, d, t: None)
    assert result.volume_id == "20260123004775F_P2"


def test_non_usb_placeholder_probe_with_usable_ata_serial_passes(tmp_path, monkeypatch):
    # 非 USB：系统枚举序列号占位但 ATA 直通真序列号可用 → 放行且用真序列号命名
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(interface_type="SATA", bridge_model="",
                                    disk_serial="0",
                                    physical_serial="REALSER123"))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    result = collect_volume(scan_root, data_root=tmp_path / "data",
                            on_disk_copy=False, smartctl=False,
                            progress_cb=lambda ph, d, t: None)
    assert result.volume_id == "REALSER123_P2"


def test_message_keywords(tmp_path, monkeypatch):
    monkeypatch.setattr("cold_manifest.collect.probe_path", _make_probe())
    msg = preflight_serial_required("/mnt/fake", manual_serial=None, smartctl=False)
    # 新规则：USB 盒 + 可用盒 ID → 放行（不再强制手填）
    assert msg is None
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="0", volume_serial_hex=""))
    msg = preflight_serial_required("/mnt/fake", manual_serial=None, smartctl=False)
    assert msg is not None
    assert "JMicron Generic SCSI Device" in msg
    assert "--serial" in msg
    assert "volume-id" in msg


# ---------------------------------------------------------------- API 层

@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(str(tmp_path / "dataroot"))
    with TestClient(app) as c:
        yield c, tmp_path


def test_api_collect_400_without_serial(client, tmp_path, monkeypatch):
    c, _ = client
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="0", volume_serial_hex=""))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    r = c.post("/api/collect", json={"path": str(scan_root)})
    assert r.status_code == 400
    assert "JMicron Generic SCSI Device" in r.json()["detail"]
    assert "--serial" in r.json()["detail"]


def test_api_collect_with_serial_submits(client, tmp_path, monkeypatch):
    c, _ = client
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="0", volume_serial_hex=""))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    r = c.post("/api/collect", json={"path": str(scan_root), "serial": "MYDISK123"})
    assert r.status_code == 201


# ---------------------------------------------------------------- CLI 层

def test_cli_collect_rc2_without_serial(tmp_path, monkeypatch, capsys):
    from cold_manifest.cli import main

    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="0", volume_serial_hex=""))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    rc = main(["collect", str(scan_root), "--data-root", str(tmp_path / "data")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "JMicron Generic SCSI Device" in err
    assert "--serial" in err


def test_cli_all_partitions_rc2_without_serial(tmp_path, monkeypatch, capsys):
    from cold_manifest.cli import main

    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _make_probe(disk_serial="0", volume_serial_hex=""))
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    rc = main(["collect", str(scan_root), "--all-partitions",
               "--data-root", str(tmp_path / "data")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--serial" in err
