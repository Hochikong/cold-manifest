"""Linux probe 身份校验（PR-B 同款口径）：smartctl 序列号只有身份验证通过才覆盖。

参照 probe/windows.py 的实现语义：
- 已验证（mapped_by ∈ {serial_match, env_override} 且无歧义）→ 覆盖 disk_serial，
  serial_source="smartctl"；
- 未验证且 probe 有值 → 保留 probe 值 + warning「为防错盘已保留探测值」；
- 未验证且 probe 空 → 补全为 serial_source="smartctl_unverified"；
- 身份歧义（identity_ambiguity 非空）→ 一律不覆盖。
"""

from __future__ import annotations

import json
import subprocess as sp

import pytest

import cold_manifest.probe.linux as lin
import cold_manifest.smart as smart
from cold_manifest.probe.linux import probe_path_linux

LSBLK_TMPL = json.dumps({
    "blockdevices": [{
        "name": "sda", "path": "/dev/sda", "type": "disk",
        "fstype": None, "label": None, "uuid": None,
        "serial": "WD-WCC123", "model": "WDC WD40EZRZ",
        "size": 4000787030016, "partuuid": None, "mountpoints": None,
        "ptuuid": "1a2b3c4d-1111-2222-3333-444455556666",
        "children": [{
            "name": "sda1", "path": "/dev/sda1", "type": "part",
            "fstype": "ext4", "label": "coldbackup", "uuid": "a-b-c",
            "serial": None, "model": None, "size": 3999998971904,
            "partuuid": "dddd-eeee", "mountpoints": ["/mnt/cold"],
        }],
    }]
})

LSBLK_NO_SERIAL_TMPL = LSBLK_TMPL.replace("WD-WCC123", "")

SMART_RAW = json.dumps({
    "device": {"name": "/dev/sda", "type": "sat", "protocol": "ATA"},
    "model_name": "WDC WD40EZRZ-00GXCB0",
    "serial_number": "WD-WCC4N7XY",
    "firmware_version": "80.00A80",
    "user_capacity": {"blocks": 7814037165, "bytes": 4000787030016},
    "smart_status": {"passed": True},
})


def _cands(device="/dev/sda"):
    return [{"device": device, "type": "sat", "source": "scan"}]


def _verbose_ok(raw=SMART_RAW, device="/dev/sda"):
    return {
        "ok": True, "raw": raw, "device": device, "device_type": "sat",
        "exit_status": 0, "reason": None, "message": None,
        "raw_excerpt": raw[:2048], "attempts": [],
        "scan_info": {"device_used": device},
    }


def _setup(monkeypatch, *, lsblk_json=LSBLK_TMPL):
    """固定 lsblk/挂载点/statvfs，隔离扫描缓存。"""
    def fake_run(cmd, *, check=True):
        assert cmd[0] == "lsblk"
        return sp.CompletedProcess(cmd, 0, stdout=lsblk_json, stderr="")

    monkeypatch.setattr(lin, "_run", fake_run)
    monkeypatch.setattr(lin, "find_mount_point",
                        lambda p: ("/mnt/cold", "/dev/sda1"))

    import os

    real_statvfs = os.statvfs

    def fake_statvfs(mp):
        return type("V", (), {"f_bavail": 1, "f_frsize": 4096,
                              "f_blocks": 100})()

    monkeypatch.setattr(lin.os, "statvfs", fake_statvfs)
    monkeypatch.setattr(smart, "reset_scan_cache", smart.reset_scan_cache)
    smart.reset_scan_cache()
    return real_statvfs


def test_verified_overrides_with_smartctl_serial(monkeypatch):
    """身份已验证（serial_match）：smartctl 序列号与 probe 不一致时按验证结果覆盖。"""
    _setup(monkeypatch)
    monkeypatch.setattr(smart, "resolve_smart_device", lambda **kw:
                        (_cands(), {"mapped_by": "serial_match",
                                    "identity_risk": ""}))
    monkeypatch.setattr(smart, "read_smart_verbose",
                        lambda dev=None, *, devices=None, **kw: _verbose_ok())
    vol, disk = probe_path_linux("/tmp")
    assert disk.identity_verified is True
    assert disk.identity_ambiguity is None
    assert disk.disk_serial == "WD-WCC4N7XY"
    assert disk.serial_source == "smartctl"
    assert disk.physical_serial == "WD-WCC4N7XY"
    assert any("不一致" in w for w in (disk.identity_warnings or []))


def test_unverified_keeps_probe_serial(monkeypatch):
    """未验证（fallback）且 probe 有值：保留 probe 值 + warning，不覆盖。"""
    _setup(monkeypatch)
    monkeypatch.setattr(smart, "resolve_smart_device", lambda **kw:
                        (_cands(), {"mapped_by": "fallback",
                                    "identity_risk": "unverified_identity"}))
    monkeypatch.setattr(smart, "read_smart_verbose",
                        lambda dev=None, *, devices=None, **kw: _verbose_ok())
    vol, disk = probe_path_linux("/tmp")
    assert disk.identity_verified is False
    assert disk.identity_risk == "unverified_identity"
    assert disk.disk_serial == "WD-WCC123"
    assert disk.serial_source == "probe"
    assert disk.physical_serial == "WD-WCC4N7XY"  # smartctl 值进 physical_serial
    assert any("保留探测值" in w for w in (disk.identity_warnings or []))


def test_unverified_empty_probe_fills_unverified(monkeypatch):
    """未验证且 probe 序列号为空：补全为 smartctl_unverified。"""
    _setup(monkeypatch, lsblk_json=LSBLK_NO_SERIAL_TMPL)
    monkeypatch.setattr(smart, "resolve_smart_device", lambda **kw:
                        (_cands(), {"mapped_by": "fallback"}))
    monkeypatch.setattr(smart, "read_smart_verbose",
                        lambda dev=None, *, devices=None, **kw: _verbose_ok())
    vol, disk = probe_path_linux("/tmp")
    assert disk.identity_verified is False
    assert disk.disk_serial == "WD-WCC4N7XY"
    assert disk.serial_source == "smartctl_unverified"
    assert any("smartctl_unverified" in w
               for w in (disk.identity_warnings or []))


def test_ambiguity_never_overrides(monkeypatch):
    """身份歧义（identity_ambiguity 非空）：即使 mapped_by=serial_match 也不覆盖。"""
    _setup(monkeypatch)
    amb = "多台设备均报告相同序列号，无法确定哪块是目标盘"
    monkeypatch.setattr(smart, "resolve_smart_device", lambda **kw:
                        (_cands(), {"mapped_by": "serial_match",
                                    "identity_ambiguity": amb}))
    monkeypatch.setattr(smart, "read_smart_verbose",
                        lambda dev=None, *, devices=None, **kw: _verbose_ok())
    vol, disk = probe_path_linux("/tmp")
    assert disk.identity_verified is False
    assert disk.identity_ambiguity == amb
    assert disk.disk_serial == "WD-WCC123"
    assert disk.serial_source == "probe"
    assert any("保留探测值" in w for w in (disk.identity_warnings or []))


def test_smart_failure_keeps_probe_and_no_identity(monkeypatch):
    """SMART 读取失败：不阻断，保留 probe 值，identity_* 保持空/None。"""
    _setup(monkeypatch)
    monkeypatch.setattr(smart, "resolve_smart_device", lambda **kw:
                        (_cands(), {"mapped_by": "fallback"}))

    def fail(dev=None, *, devices=None, **kw):
        return {"ok": False, "raw": None, "device": "", "device_type": "",
                "exit_status": None, "reason": "permission_denied",
                "message": smart.REASON_MESSAGES["permission_denied"],
                "raw_excerpt": "Access denied", "attempts": [],
                "scan_info": {}}

    monkeypatch.setattr(smart, "read_smart_verbose", fail)
    vol, disk = probe_path_linux("/tmp")
    assert disk.disk_serial == "WD-WCC123"
    assert disk.serial_source == "probe"
    assert disk.smart_status == "unavailable"
    assert "管理员" in (disk.smart_error or "")
    assert disk.identity_verified is False
    assert not (disk.identity_warnings or [])


def test_env_override_is_verified(monkeypatch):
    """CLDM_SMARTCTL_DEVICE 覆盖（env_override）视为已验证：允许覆盖序列号。"""
    _setup(monkeypatch)
    monkeypatch.setattr(smart, "resolve_smart_device", lambda **kw:
                        (_cands(), {"mapped_by": "env_override"}))
    monkeypatch.setattr(smart, "read_smart_verbose",
                        lambda dev=None, *, devices=None, **kw: _verbose_ok())
    vol, disk = probe_path_linux("/tmp")
    assert disk.identity_verified is True
    assert disk.disk_serial == "WD-WCC4N7XY"
    assert disk.serial_source == "smartctl"


def test_resolve_receives_expected_identity(monkeypatch):
    """resolve_smart_device 收到 probe 的序列号/容量/型号（身份解析入参）。"""
    _setup(monkeypatch)
    seen: dict = {}

    def fake_resolve(**kw):
        seen.update(kw)
        return [], {"mapped_by": "fallback"}

    monkeypatch.setattr(smart, "resolve_smart_device", fake_resolve)
    monkeypatch.setattr(smart, "read_smart_verbose",
                        lambda dev=None, *, devices=None, **kw:
                        {"ok": False, "raw": None, "device": "",
                         "device_type": "", "exit_status": None,
                         "reason": "not_found",
                         "message": smart.REASON_MESSAGES["not_found"],
                         "raw_excerpt": "", "attempts": [], "scan_info": {}})
    probe_path_linux("/tmp")
    assert seen["expected_serial"] == "WD-WCC123"
    assert seen["expected_capacity_bytes"] == 4000787030016
    assert seen["expected_model"] == "WDC WD40EZRZ"


@pytest.mark.parametrize("manual", ["MY-SERIAL-1"])
def test_manual_serial_wins_over_everything(monkeypatch, manual):
    """--serial 手动指定优先级最高：覆盖任何 smartctl/身份结论。"""
    _setup(monkeypatch)
    monkeypatch.setattr(smart, "resolve_smart_device", lambda **kw:
                        (_cands(), {"mapped_by": "serial_match"}))
    monkeypatch.setattr(smart, "read_smart_verbose",
                        lambda dev=None, *, devices=None, **kw: _verbose_ok())
    vol, disk = probe_path_linux("/tmp", manual_serial=manual)
    assert disk.disk_serial == manual
    assert disk.serial_source == "manual"
