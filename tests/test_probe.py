"""probe 层单元测试：夹具 JSON 解析、命令构造、ProbeError 路径。"""

from __future__ import annotations

import json

import pytest

from cold_manifest.probe import DiskInfo, ProbeError, VolumeInfo
from cold_manifest.probe.linux import (
    parse_lsblk,
    parse_smartctl,
    select_target,
    _table_type,
)
from cold_manifest.probe.windows import build_powershell_command, parse_windows_json

LSBLK_JSON = json.dumps(
    {
        "blockdevices": [
            {
                "name": "sda",
                "path": "/dev/sda",
                "type": "disk",
                "fstype": None,
                "label": None,
                "uuid": None,
                "serial": "WD-WCC123",
                "model": "WDC WD40EZRZ",
                "size": 4000787030016,
                "partuuid": None,
                "mountpoints": None,
                "ptuuid": "1a2b3c4d-1111-2222-3333-444455556666",
                "children": [
                    {
                        "name": "sda1",
                        "path": "/dev/sda1",
                        "type": "part",
                        "fstype": "ext4",
                        "label": "coldbackup",
                        "uuid": "aaaa-bbbb-cccc",
                        "serial": None,
                        "model": None,
                        "size": 3999998971904,
                        "partuuid": "dddd-eeee-1111-2222",
                        "mountpoints": ["/mnt/cold"],
                    },
                    {
                        "name": "sda2",
                        "path": "/dev/sda2",
                        "type": "part",
                        "fstype": "ntfs",
                        "label": "win",
                        "uuid": "9A0B",
                        "serial": None,
                        "model": None,
                        "size": 787030016,
                        "partuuid": "ffff-0000-1111-2222",
                        "mountpoints": ["/mnt/win"],
                    },
                ],
            }
        ]
    }
)

SMARTCTL_JSON = json.dumps(
    {
        "device": {"name": "/dev/sda", "type": "sat", "protocol": "ATA"},
        "model_name": "WDC WD40EZRZ-00GXCB0",
        "serial_number": "WD-WCC4N7XY",
        "firmware_version": "80.00A80",
        "user_capacity": {"blocks": 7814037165, "bytes": 4000787030016},
        "smart_status": {"passed": True},
        "temperature": {"current": 31},
        "power_on_time": {"hours": 12034},
        "reallocated_sector_count": {"raw": {"value": 0}},
    }
)

PS_JSON = json.dumps(
    {
        "volume": {
            "fs": "NTFS",
            "label": "冷备 E",
            "vserial": "1A2B3C4D",
            "size": 2000397795328,
            "free": 512110190592,
        },
        "partition": {
            "guid": "{1a2b3c4d-1111-2222-3333-444455556666}",
            "index": 2,
            "offset": 1048576,
            "size": 2000397795328,
            "mbrType": 0,
            "gptType": "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}",
        },
        "disk": {
            "index": 1,
            "model": "TOSHIBA External USB 3.0",
            "serial": "X0FGBM2AS",
            "interface": "USB",
            "firmware": "AR00A",
            "size": 2000398934016,
            "friendlyName": "TOSHIBA External USB 3.0 USB Device",
            "busType": "USB",
            "partitionStyle": "GPT",
        },
    }
)


class TestLinux:
    def test_parse_lsblk_flat(self):
        nodes = parse_lsblk(LSBLK_JSON)
        names = [n["name"] for n in nodes]
        assert names == ["sda", "sda1", "sda2"]
        sda1 = nodes[1]
        assert sda1["parent"]["name"] == "sda"

    def test_select_target_by_device(self):
        nodes = parse_lsblk(LSBLK_JSON)
        part, disk = select_target(nodes, "/dev/sda1", "/mnt/cold")
        assert part["name"] == "sda1"
        assert disk["name"] == "sda"

    def test_select_target_by_mountpoint(self):
        nodes = parse_lsblk(LSBLK_JSON)
        part, disk = select_target(nodes, "/dev/whatever-does-not-match", "/mnt/win")
        assert part["name"] == "sda2"

    def test_select_target_missing(self):
        nodes = parse_lsblk(LSBLK_JSON)
        with pytest.raises(ProbeError, match="未找到"):
            select_target(nodes, "/dev/sdz9", "/nonexistent")

    def test_parse_smartctl(self):
        sj = parse_smartctl(SMARTCTL_JSON)
        assert sj["model_name"] == "WDC WD40EZRZ-00GXCB0"
        assert sj["smart_status"]["passed"] is True

    def test_parse_smartctl_bad_json(self):
        with pytest.raises(ProbeError, match="smartctl"):
            parse_smartctl("not json {")

    def test_parse_lsblk_bad_json(self):
        with pytest.raises(ProbeError, match="lsblk"):
            parse_lsblk("{{{")

    def test_table_type(self):
        nodes = parse_lsblk(LSBLK_JSON)
        part, disk = select_target(nodes, "/dev/sda1", "/mnt/cold")
        assert _table_type(part, disk) == "GPT"
        mbr_disk = {"ptuuid": "1a2b3c4d", "type": "disk"}
        assert _table_type({"partuuid": ""}, mbr_disk) == "MBR"
        assert _table_type({}, None) == "unknown"


class TestWindows:
    def test_build_command(self):
        script = build_powershell_command("e:")
        assert "$letter = 'E'" in script
        assert "DeviceID" in script
        assert "Win32_LogicalDisk" in script
        assert "Get-Partition" in script
        assert "ConvertTo-Json" in script

    def test_build_command_invalid(self):
        with pytest.raises(ProbeError):
            build_powershell_command("UNC")

    def test_parse_windows_json(self):
        vol, disk = parse_windows_json(PS_JSON, "E")
        assert isinstance(vol, VolumeInfo)
        assert isinstance(disk, DiskInfo)
        assert vol.filesystem == "NTFS"
        assert vol.label == "冷备 E"
        assert vol.volume_serial_hex == "1A2B3C4D"
        assert vol.partition_uuid == "{1a2b3c4d-1111-2222-3333-444455556666}"
        assert vol.partition_index == 2
        assert vol.partition_table_type == "GPT"
        assert vol.capacity_bytes == 2000397795328
        assert vol.free_bytes == 512110190592
        assert vol.mount_point == "E:\\"
        assert disk.disk_serial == "X0FGBM2AS"
        assert disk.serial_source == "probe"
        assert disk.bridge_model == "TOSHIBA External USB 3.0"
        assert disk.interface_type == "USB"
        assert disk.firmware == "AR00A"
        assert disk.smart_status == "unavailable"

    def test_parse_null_fields(self):
        sparse = json.dumps(
            {
                "volume": None,
                "partition": {"guid": None, "index": None},
                "disk": {"model": None, "serial": None},
            }
        )
        vol, disk = parse_windows_json(sparse, "f")
        assert vol.filesystem == ""
        assert vol.volume_serial_hex == ""
        assert vol.partition_uuid == ""
        assert vol.partition_index is None
        assert vol.partition_table_type == "unknown"
        assert disk.disk_serial == ""
        assert disk.serial_source == ""

    def test_parse_bad_json(self):
        with pytest.raises(ProbeError, match="PowerShell"):
            parse_windows_json("err on stderr", "E")


class TestDispatch:
    def test_unsupported_platform(self, monkeypatch):
        import cold_manifest.probe as probe

        monkeypatch.setattr(probe.sys, "platform", "darwin")
        with pytest.raises(ProbeError, match="平台"):
            probe.probe_path("/tmp")

    def test_path_not_exists_linux(self, monkeypatch):
        import cold_manifest.probe as probe

        monkeypatch.setattr(probe.sys, "platform", "linux")
        with pytest.raises(ProbeError, match="路径不存在"):
            probe.probe_path("/definitely/not/exist-xyz")

    # ---- Gate P1.1a：整盘/LUKS 与 smartctl 不阻断 ----

    WHOLE_DISK_JSON = json.dumps(
        {
            "blockdevices": [
                {
                    "name": "sdb",
                    "path": "/dev/sdb",
                    "type": "disk",
                    "fstype": "crypto_LUKS",
                    "label": None,
                    "uuid": "aaaa-bbbb-cccc",
                    "serial": "LUKS-DISK-1",
                    "model": "TOSHIBA HDWD120",
                    "size": 2000398934016,
                    "partuuid": None,
                    "mountpoints": ["/mnt/luks"],
                    "ptuuid": None,
                    "children": [],
                }
            ]
        }
    )

    def test_select_target_whole_disk(self):
        """整盘文件系统（无分区，如 LUKS/出厂格式盘）：disk 节点同时充当分区与磁盘。"""
        nodes = parse_lsblk(self.WHOLE_DISK_JSON)
        part, disk = select_target(nodes, "/dev/sdb", "/mnt/luks")
        assert part["name"] == "sdb"
        assert disk is part

    def _run_probe_linux(self, monkeypatch, fake_run):
        """在受控 _run 下跑 probe_path_linux（tmp 目录 → / 挂载点）。"""
        import subprocess as sp

        import cold_manifest.probe.linux as lin

        monkeypatch.setattr(lin, "_run", fake_run)
        monkeypatch.setattr(lin, "find_mount_point", lambda p: ("/mnt/cold", "/dev/sda1"))
        monkeypatch.setattr(lin.os, "statvfs", lambda mp: type(
            "V", (), {"f_bavail": 1, "f_frsize": 4096, "f_blocks": 100})())
        return lin.probe_path_linux("/tmp")

    def test_smartctl_missing_not_blocking(self, monkeypatch):
        """smartctl 未安装（FileNotFoundError）不阻断，status=unavailable。

        P4-② 起 smartctl 调用走 smart.read_smart（smart._run_cmd），不再复用
        probe.linux._run。
        """
        import subprocess as sp

        calls = []

        def fake_run(cmd, *, check=True):
            calls.append(cmd[0])
            assert cmd[0] == "lsblk"
            return sp.CompletedProcess(cmd, 0, stdout=LSBLK_JSON, stderr="")

        def fake_smart_run(cmd):
            calls.append(cmd[0])
            return None, "not_found"  # smartctl 不可用（_run_cmd_ex 折叠为 (None, err)）

        monkeypatch.setattr("cold_manifest.smart._run_cmd_ex", fake_smart_run)
        vol, disk = self._run_probe_linux(monkeypatch, fake_run)
        assert "smartctl" in calls
        assert disk.smart_status == "unavailable"
        assert disk.smart_raw is None
        assert disk.disk_serial == "WD-WCC123"  # lsblk 序号兜底

    def test_smartctl_timeout_not_blocking(self, monkeypatch):
        import subprocess as sp

        def fake_run(cmd, *, check=True):
            if cmd[0] == "smartctl":
                raise ProbeError("外部工具超时：smartctl")
            return sp.CompletedProcess(cmd, 0, stdout=LSBLK_JSON, stderr="")

        def fake_smart_run(cmd):
            return None, "timeout"  # 超时折叠为 (None, err)

        monkeypatch.setattr("cold_manifest.smart._run_cmd_ex", fake_smart_run)
        vol, disk = self._run_probe_linux(monkeypatch, fake_run)
        assert disk.smart_status == "unavailable"

    def test_smartctl_raw_stored(self, monkeypatch):
        """S2：smartctl 原始 stdout 存入 smart_raw。"""
        import subprocess as sp

        def fake_run(cmd, *, check=True):
            assert cmd[0] == "lsblk"
            return sp.CompletedProcess(cmd, 0, stdout=LSBLK_JSON, stderr="")

        def fake_smart_run(cmd):
            assert cmd[0] == "smartctl"
            assert cmd[1:3] == ["-i", "-H"]
            return sp.CompletedProcess(cmd, 0, stdout=SMARTCTL_JSON, stderr=""), None

        monkeypatch.setattr("cold_manifest.smart._run_cmd_ex", fake_smart_run)
        vol, disk = self._run_probe_linux(monkeypatch, fake_run)
        assert disk.smart_status == "passed"
        assert disk.smart_raw == SMARTCTL_JSON
        assert disk.physical_model == "WDC WD40EZRZ-00GXCB0"
