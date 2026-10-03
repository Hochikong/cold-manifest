"""SMART 后端（P4-②）：解析 / smartctl 调用 / disk_smart 历史 / 采集链路。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cold_manifest import smart
from cold_manifest.catalog import connect_catalog, ensure_disk, ensure_volume, register_snapshot
from cold_manifest.collect import collect_volume
from cold_manifest.db import init_snapshot
from cold_manifest.probe import DiskInfo, VolumeInfo

SAMPLE = json.dumps({
    "device": {"name": "/dev/sdb", "type": "sat", "protocol": "ATA"},
    "model_family": "Toshiba N300",
    "model_name": "TOSHIBA HDWG480",
    "serial_number": "  9876ABC  ",
    "firmware_version": "GX2A",
    "smart_status": {"passed": True},
    "temperature": {"current": 31},
    "power_on_time": {"hours": 12345},
    "ata_smart_attributes": {"table": [
        {"id": 3, "name": "Spin_Up_Time", "raw": {"value": 4500}},
        {"id": 4, "name": "Start_Stop_Count", "raw": {"value": 210}},
        {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 8}},
        {"id": 9, "name": "Power_On_Hours", "raw": {"value": 12345}},
        {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 4}},
    ]},
})

SAMPLE_FAILED = json.dumps({"smart_status": {"passed": False}})


# ---------------------------------------------------------------- parse_smart


def test_parse_smart_full() -> None:
    p = smart.parse_smart(SAMPLE)
    assert p["health"] == "passed"
    assert p["temperature_c"] == 31
    assert p["power_on_hours"] == 12345
    assert p["reallocated_ct"] == 8
    assert p["pending_ct"] == 4
    assert p["start_stop_ct"] == 210
    assert p["spin_up_ms"] == 4500
    assert p["device_type"] == "sat"
    assert p["model"] == "TOSHIBA HDWG480"
    assert p["serial"] == "9876ABC"
    assert p["firmware"] == "GX2A"


def test_parse_smart_failed_and_missing() -> None:
    assert smart.parse_smart(SAMPLE_FAILED)["health"] == "failed"
    p = smart.parse_smart("{}")
    assert p["health"] == "unavailable"
    for k in smart.SMART_KEYS:
        if k != "health":
            assert p[k] is None


def test_parse_smart_bad_input_never_raises() -> None:
    for bad in (None, "", "not json {{{", "[1,2,3]", 12345):
        p = smart.parse_smart(bad)
        assert p["health"] == "unavailable"
    # 属性 raw 不是数字也不抛
    p = smart.parse_smart(json.dumps(
        {"ata_smart_attributes": {"table": [{"id": 5, "raw": {"value": "x"}}]}}))
    assert p["reallocated_ct"] is None


def test_parse_smart_power_on_hours_fallback_attr9() -> None:
    raw = json.dumps({"ata_smart_attributes": {"table": [
        {"id": 9, "raw": {"value": 0x1234_5678}}  # 只取低 16 位
    ]}})
    assert smart.parse_smart(raw)["power_on_hours"] == 0x5678


# ---------------------------------------------------------------- read_smart


def _fake_cmd(results: list[object]):
    """按调用序返回预设 CompletedProcess 样子；记录 cmd；耗尽后重复最后一个。"""
    calls: list[list[str]] = []

    def fake(cmd: list[str]):
        calls.append(cmd)
        r = results[min(len(calls) - 1, len(results) - 1)]
        if r is OSError:
            raise OSError("not found")
        return r

    return fake, calls


def _proc(stdout: str = "", rc: int = 0):
    import subprocess

    p = subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")
    return p


def test_read_smart_sat_retry(monkeypatch) -> None:
    fake, calls = _fake_cmd([
        _proc("smartctl says no", rc=2),   # 默认参数：无 JSON 输出
        _proc(SAMPLE, rc=0),               # -d sat 重试成功
    ])
    monkeypatch.setattr(smart, "_run_cmd", fake)
    res = smart.read_smart("/dev/sdb")
    assert res is not None
    assert res["device_type"] == "sat"
    assert json.loads(res["raw"])["model_name"] == "TOSHIBA HDWG480"
    assert calls[0][-1] == "/dev/sdb"
    assert "-d" in calls[1] and "sat" in calls[1]
    # 参数顺序：-d sat 在 -i 之前（smartctl 要求 -d 靠前）
    assert calls[1].index("-d") < calls[1].index("-i")


def test_read_smart_first_try_ok(monkeypatch) -> None:
    fake, calls = _fake_cmd([_proc(SAMPLE, rc=0)])
    monkeypatch.setattr(smart, "_run_cmd", fake)
    res = smart.read_smart("/dev/sdb")
    assert res is not None and res["device_type"] == ""
    assert len(calls) == 1


def test_read_smart_unavailable_returns_none(monkeypatch) -> None:
    fake, _ = _fake_cmd([_proc("", rc=2), _proc("", rc=2)])
    monkeypatch.setattr(smart, "_run_cmd", fake)
    assert smart.read_smart("/dev/sdb") is None


def test_read_smart_config(monkeypatch) -> None:
    """CLDM_SMARTCTL / CLDM_SMARTCTL_ARGS（v1/v2 参数）透传。"""
    fake, calls = _fake_cmd([_proc(SAMPLE, rc=0)])
    monkeypatch.setattr(smart, "_run_cmd", fake)
    monkeypatch.setenv("CLDM_SMARTCTL", "/opt/tools/smartctl")
    monkeypatch.setenv("CLDM_SMARTCTL_ARGS", "--nocheck standby")
    assert smart.smartctl_exec() == "/opt/tools/smartctl"
    assert smart.read_smart("/dev/sdb") is not None
    assert calls[0][:3] == ["/opt/tools/smartctl", "--nocheck", "standby"]


# ---------------------------------------------------------------- read_smart_verbose


def _fake_cmd_ex(results: list[object]):
    """按调用序返回预设 (proc, err)；记录 cmd；耗尽后重复最后一个。"""
    calls: list[list[str]] = []

    def fake(cmd: list[str]):
        calls.append(cmd)
        r = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(r, tuple) and r and r[0] == "ERR":
            return None, r[1]
        return r, None

    return fake, calls


def test_read_smart_verbose_success_first_try(monkeypatch) -> None:
    fake, calls = _fake_cmd_ex([_proc(SAMPLE, rc=0)])
    monkeypatch.setattr(smart, "_run_cmd_ex", fake)
    res = smart.read_smart_verbose("/dev/sdb")
    assert res["ok"] is True
    assert res["device_type"] == ""
    assert res["reason"] is None
    assert json.loads(res["raw"])["model_name"] == "TOSHIBA HDWG480"
    assert len(calls) == 1


def test_read_smart_verbose_sat_retry(monkeypatch) -> None:
    fake, calls = _fake_cmd_ex([_proc("", rc=2), _proc(SAMPLE, rc=0)])
    monkeypatch.setattr(smart, "_run_cmd_ex", fake)
    res = smart.read_smart_verbose("/dev/sdb")
    assert res["ok"] is True and res["device_type"] == "sat"
    # 默认参数失败的尝试也留下（诊断用）
    assert len(res["attempts"]) == 1
    assert res["attempts"][0]["device_type"] == "default"
    assert res["attempts"][0]["ok"] is False
    assert res["attempts"][0]["argv"].endswith("/dev/sdb")


def test_read_smart_verbose_permission_denied(monkeypatch) -> None:
    import subprocess

    def fake2(cmd):
        p = subprocess.CompletedProcess(args=[], returncode=16,
                                        stdout="", stderr="Open failed: Access is denied")
        return p, None

    monkeypatch.setattr(smart, "_run_cmd_ex", fake2)
    res = smart.read_smart_verbose("/dev/sdb")
    assert res["ok"] is False
    assert res["reason"] == "permission_denied"
    assert "管理员" in res["message"]
    # 类型兜底链全试过（auto → sat → sat,12 → 桥专用…）
    assert len(res["attempts"]) == len(smart._device_types())
    assert res["attempts"][0]["rc"] == 16
    assert "Access is denied" in res["attempts"][0]["stderr_excerpt"]
    assert "Access is denied" in res["raw_excerpt"]


def test_read_smart_verbose_no_smart(monkeypatch) -> None:
    import subprocess

    def fake2(cmd):
        # rc=4（bit2：部分子命令失败）+ 无 JSON → 不算读到数据；
        # stderr 指向设备类型识别失败（bit0/bit1 未置位才走到文案归类）
        p = subprocess.CompletedProcess(args=[], returncode=4, stdout="",
                                        stderr="Unable to detect device type")
        return p, None

    monkeypatch.setattr(smart, "_run_cmd_ex", fake2)
    res = smart.read_smart_verbose("/dev/sdb")
    assert res["ok"] is False and res["reason"] == "device_type_unknown"
    assert "无法识别设备类型" in res["message"]


def test_read_smart_verbose_not_found(monkeypatch) -> None:
    fake, _ = _fake_cmd_ex([("ERR", "not_found"), ("ERR", "not_found")])
    monkeypatch.setattr(smart, "_run_cmd_ex", fake)
    res = smart.read_smart_verbose("/dev/nope")
    assert res["ok"] is False and res["reason"] == "not_found"
    assert res["raw_excerpt"] == ""


def test_read_smart_verbose_timeout(monkeypatch) -> None:
    fake, _ = _fake_cmd_ex([("ERR", "timeout"), ("ERR", "timeout")])
    monkeypatch.setattr(smart, "_run_cmd_ex", fake)
    res = smart.read_smart_verbose("/dev/sdb")
    assert res["ok"] is False and res["reason"] == "timeout"
    assert "超时" in res["message"]


def test_read_smart_verbose_other(monkeypatch) -> None:
    fake, _ = _fake_cmd_ex([_proc("", rc=4), _proc("", rc=4)])
    monkeypatch.setattr(smart, "_run_cmd_ex", fake)
    res = smart.read_smart_verbose("/dev/sdb")
    assert res["ok"] is False and res["reason"] == "other"
    assert res["message"]


def test_read_smart_verbose_device_open_bit1(monkeypatch) -> None:
    """exit_status bit1(2) → device_open，文案给可执行建议。"""
    import json as _json
    import subprocess

    def fake2(cmd):
        # USB 桥实测形态：rc=2，stdout 仍是 JSON，原因在 messages[].string
        out = _json.dumps({"smartctl": {"exit_status": 2}, "messages": [
            {"string": "Smartctl open device: Invalid argument"}]})
        p = subprocess.CompletedProcess(args=[], returncode=2,
                                        stdout=out, stderr="")
        return p, None

    monkeypatch.setattr(smart, "_run_cmd_ex", fake2)
    res = smart.read_smart_verbose("\\\\.\\PhysicalDrive2")
    assert res["ok"] is False and res["reason"] == "device_open"
    assert "/dev/sdN" in res["message"]
    assert "CLDM_SMARTCTL_DEVICE" in res["message"]
    # messages[].string 已提取进 attempts（-j 出错时 stderr 为空）
    assert "Invalid argument" in res["attempts"][0]["stdout_messages"]
    assert "Invalid argument" in res["raw_excerpt"]


def test_read_smart_verbose_cmdline_error_bit0(monkeypatch) -> None:
    import subprocess

    def fake2(cmd):
        p = subprocess.CompletedProcess(args=[], returncode=1, stdout="",
                                        stderr="Unknown argument: -x")
        return p, None

    monkeypatch.setattr(smart, "_run_cmd_ex", fake2)
    res = smart.read_smart_verbose("/dev/sdb")
    assert res["ok"] is False and res["reason"] == "cmdline_error"
    assert "bit0" in res["message"]


def test_read_smart_verbose_device_candidates_chain(monkeypatch) -> None:
    """候选链逐台设备尝试；attempts 记录设备串；scan_info 带映射结论。"""
    fake, calls = _fake_cmd_ex([_proc("", rc=4)])
    monkeypatch.setattr(smart, "_run_cmd_ex", fake)
    monkeypatch.setattr(smart, "_SCAN_CACHE", [])
    res = smart.read_smart_verbose(devices=[
        {"device": "/dev/sdc", "type": "sat", "source": "scan"},
        {"device": "\\\\.\\PhysicalDrive2", "type": "", "source": "fallback-pd"},
    ])
    assert res["ok"] is False
    assert res["device_candidates"] == ["/dev/sdc", "\\\\.\\PhysicalDrive2"]
    devices_tried = {a["device"] for a in res["attempts"]}
    assert devices_tried == {"/dev/sdc", "\\\\.\\PhysicalDrive2"}
    assert all(a["argv"].split()[-1] == a["device"] for a in res["attempts"])
    si = res["scan_info"]
    assert si["devices"] == []
    assert si["device_used"] is None
    assert si["mapped_from_scan"] is True  # 候选来源标记为 scan
    assert [c["device"] for c in si["candidates"]] == \
        ["/dev/sdc", "\\\\.\\PhysicalDrive2"]
    # 类型兜底链按设备各走一遍
    assert len(res["attempts"]) == 2 * len(smart._device_types("sat"))


def test_read_smart_verbose_success_has_scan_info(monkeypatch) -> None:
    fake, _ = _fake_cmd_ex([_proc(SAMPLE, rc=0)])
    monkeypatch.setattr(smart, "_run_cmd_ex", fake)
    monkeypatch.setattr(smart, "_SCAN_CACHE", [
        {"device": "/dev/sdb", "type": "sat"}])
    res = smart.read_smart_verbose("/dev/sdb", suggested_type="sat")
    assert res["ok"] is True
    assert res["device"] == "/dev/sdb"
    assert res["scan_info"]["device_used"] == "/dev/sdb"
    assert res["scan_info"]["devices"] == [{"device": "/dev/sdb", "type": "sat"}]


# ---------------------------------------------------------------- Windows USB 桥实机形态


def test_windows_type_chain_sat_before_auto() -> None:
    """Windows 通路：sat 先于 auto（auto 会把 USB 桥误判成 jmb39x 报
    Invalid argument），auto 只在末尾兜底；扫描建议类型最优先。"""
    chain = smart._device_types("sat", windows=True)
    assert chain[0] == "sat"
    assert chain[-1] == ""
    assert chain.index("sat") < chain.index("")
    assert "sat,12" in chain
    # Linux 顺序不变：auto 仍最前
    assert smart._device_types("sat")[0] == ""


def test_read_smart_windows_chain_order(monkeypatch) -> None:
    """PhysicalDrive 形态的设备走 Windows 链：第一个尝试就是 -d sat。"""
    fake, calls = _fake_cmd([_proc(SAMPLE, rc=0)])
    monkeypatch.setattr(smart, "_run_cmd", fake)
    res = smart.read_smart("\\\\.\\PhysicalDrive2")
    assert res is not None and res["device_type"] == "sat"
    assert len(calls) == 1
    assert calls[0][1:3] == ["-d", "sat"]
    assert calls[0][-1] == "\\\\.\\PhysicalDrive2"


def test_read_smart_verbose_admin_rights_hint(monkeypatch) -> None:
    """非管理员时 smartctl stderr 提示 limited functionality → permission_denied。"""
    import subprocess

    def fake2(cmd):
        return subprocess.CompletedProcess(
            args=[], returncode=2, stdout="",
            stderr="Limited functionality due to missing admin rights"), None

    monkeypatch.setattr(smart, "_run_cmd_ex", fake2)
    res = smart.read_smart_verbose("/dev/sdc")
    assert res["ok"] is False
    assert res["reason"] == "permission_denied"
    assert "管理员" in res["message"]


def test_read_smart_verbose_error5_is_permission_denied(monkeypatch) -> None:
    """实机 USB 桥形态：rc bit1(2) + messages "Open failed, Error=5"
    → permission_denied（不再误报 device_open），message 指向管理员启动。"""
    import subprocess

    def fake2(cmd):
        out = json.dumps({"smartctl": {"exit_status": 2}, "messages": [
            {"string": "Smartctl open device: \\\\.\\PhysicalDrive2 [SAT] failed:"
                       " \\\\.\\PhysicalDrive2: Open failed, Error=5"}]})
        return subprocess.CompletedProcess(args=[], returncode=2,
                                           stdout=out, stderr=""), None

    monkeypatch.setattr(smart, "_run_cmd_ex", fake2)
    res = smart.read_smart_verbose("\\\\.\\PhysicalDrive2")
    assert res["ok"] is False
    assert res["reason"] == "permission_denied"
    assert "管理员" in res["message"]
    assert "NVMe" in res["message"]
    assert "-d sat" in res["message"]


def test_read_smart_verbose_no_pd_retry(monkeypatch) -> None:
    """调研定案：\\\\.\\PhysicalDriveN 不进候选链、失败后也不动态追加——
    sd 报"设备不存在"时就地失败（pd 形态裸传本来就是 EINVAL）。"""
    monkeypatch.setattr(smart, "_run_cmd_ex",
                        lambda cmd: (_proc("", rc=2), "not_found"))
    monkeypatch.setattr(smart, "_SCAN_CACHE", [])
    res = smart.read_smart_verbose(devices=[
        {"device": "/dev/sdc", "type": "sat", "source": "scan"}])
    assert res["ok"] is False and res["reason"] == "not_found"
    assert {a["device"] for a in res["attempts"]} == {"/dev/sdc"}
    assert [c["device"] for c in res["scan_info"]["candidates"]] == ["/dev/sdc"]


def test_read_smart_verbose_scan_info_note_passthrough(monkeypatch) -> None:
    """候选的 note（同盘合并/动态追加原因）透传到 scan_info.candidates。"""
    fake, _ = _fake_cmd_ex([_proc("", rc=4)])
    monkeypatch.setattr(smart, "_run_cmd_ex", fake)
    monkeypatch.setattr(smart, "_SCAN_CACHE", [])
    res = smart.read_smart_verbose(devices=[
        {"device": "/dev/sdc", "type": "sat", "source": "scan",
         "note": "pd 形态已合并"}])
    assert res["scan_info"]["candidates"][0]["note"] == "pd 形态已合并"


# ---------------------------------------------------------------- 假 smartctl 实测脚本（真子进程）


def _write_fake_smartctl(tmp_path: Path, mode: str) -> str:
    """生成可执行的假 smartctl（bash）：mode 决定失败剧本。

    - auto_jmb39x：无 -d 或非 sat 的 -d → jmb39x Invalid argument（rc 2）；
      -d sat → 真 JSON（rc 0）——复现实机"auto 误判、sat 能读"；
    - error5：一律 Error=5 Open failed（rc 2）——权限不足形态。
    """
    script = tmp_path / f"fake_smartctl_{mode}.sh"
    if mode == "auto_jmb39x":
        body = r'''
if [ "$1" = "-d" ] && [ "$2" = "sat" ]; then
  echo '__SAMPLE__'
  exit 0
fi
echo '{"smartctl": {"exit_status": 2}, "messages": [{"string": "Smartctl open device: jmb39x_disk_0 failed: Invalid argument"}]}'
exit 2
'''
        body = body.replace("__SAMPLE__", SAMPLE)
    else:
        body = (
            'echo \'{"smartctl": {"exit_status": 2}, "messages": '
            '[{"string": "Smartctl open device: failed: Open failed, Error=5"}]}\'\n'
            "exit 2\n"
        )
    script.write_text("#!/bin/bash\n" + body)
    script.chmod(0o755)
    return str(script)


def test_fake_smartctl_auto_jmb39x_then_sat(tmp_path: Path,
                                            monkeypatch) -> None:
    """假 smartctl 场景①：auto 误判 jmb39x + Invalid argument → 后续 sat
    成功；尝试顺序里 sat 在 auto 之前（Windows 通路）。"""
    monkeypatch.setenv("CLDM_SMARTCTL",
                       _write_fake_smartctl(tmp_path, "auto_jmb39x"))
    monkeypatch.delenv("CLDM_SMARTCTL_ARGS", raising=False)
    monkeypatch.delenv("CLDM_SMARTCTL_DEVICE", raising=False)
    monkeypatch.setattr(smart, "_SCAN_CACHE", [
        {"device": "/dev/sda", "type": "nvme"},
        {"device": "/dev/sdb", "type": "nvme"},
        {"device": "/dev/sdc", "type": "sat"},
    ])
    cands = smart.smart_device_candidates(2)
    # 场景③/④：扫描给出 sat 类型且同盘去重后只有一条候选
    assert len(cands) == 1
    assert cands[0]["device"] == "/dev/sdc" and cands[0]["type"] == "sat"
    res = smart.read_smart_verbose(devices=[
        {"device": "\\\\.\\PhysicalDrive2", "type": "", "source": "scan"}])
    assert res["ok"] is True
    assert res["device_type"] == "sat"
    # Windows 通路 sat 最优先：auto（jmb39x 误判）根本没被尝试
    assert res["attempts"] == []
    # 对照：Linux 通路（/dev/sdc）顺序不变，auto 先试失败后 sat 兜住
    res2 = smart.read_smart_verbose("/dev/sdc", suggested_type="sat")
    assert res2["ok"] is True and res2["device_type"] == "sat"
    tried = [a["device_type"] for a in res2["attempts"]]
    assert tried[0] == "default"        # auto 失败（jmb39x 误判）
    assert "sat" not in tried           # sat 成功，不进失败尝试
    assert "jmb39x" in res2["attempts"][0]["stdout_messages"]


def test_fake_smartctl_error5_permission_denied(tmp_path: Path,
                                                monkeypatch) -> None:
    """假 smartctl 场景②：Error=5 → permission_denied，message 含"管理员"。"""
    monkeypatch.setenv("CLDM_SMARTCTL",
                       _write_fake_smartctl(tmp_path, "error5"))
    monkeypatch.delenv("CLDM_SMARTCTL_ARGS", raising=False)
    res = smart.read_smart_verbose("\\\\.\\PhysicalDrive2")
    assert res["ok"] is False
    assert res["reason"] == "permission_denied"
    assert "管理员" in res["message"]
    assert "-d sat" in res["message"]


# ---------------------------------------------------------------- exit_status 位掩码 / 设备名 / 类型兜底链

# 实测三行 scan 输出形态（Windows USB 桥盘为第三行）
SCAN_TEXT = (
    "/dev/sda -d ata # /dev/sda, ATA device\n"
    "/dev/sdb -d scsi # /dev/sdb, SCSI device\n"
    "/dev/sdc -d sat # [SAT], ATA device\n"
)


def test_parse_scan_output() -> None:
    entries = smart.parse_scan_output(SCAN_TEXT)
    assert [e["device"] for e in entries] == ["/dev/sda", "/dev/sdb", "/dev/sdc"]
    assert [e["type"] for e in entries] == ["ata", "scsi", "sat"]


# 实测 smartctl 7.5 `--scan -j`（Windows 宿主，两块 NVMe）——JSON 形态
SCAN_JSON = json.dumps({
    "json_format_version": [1, 0],
    "smartctl": {"version": [7, 5], "exit_status": 0},
    "devices": [
        {"name": "/dev/sda", "info_name": "/dev/sda",
         "type": "nvme", "protocol": "NVMe"},
        {"name": "/dev/sdb", "info_name": "/dev/sdb",
         "type": "nvme", "protocol": "NVMe"},
    ],
})


def test_parse_scan_output_json() -> None:
    """`--scan -j` 的 JSON 形态必须能解析（曾因只认文本导致映射成空）。"""
    entries = smart.parse_scan_output(SCAN_JSON)
    assert [e["device"] for e in entries] == ["/dev/sda", "/dev/sdb"]
    assert [e["type"] for e in entries] == ["nvme", "nvme"]


def test_parse_scan_output_json_info_name_fallback() -> None:
    raw = json.dumps({"devices": [
        {"info_name": "/dev/sdz", "type": "sat"},          # 缺 name → info_name
        {"name": "/dev/sdy"},                              # 缺 type → ""
    ]})
    entries = smart.parse_scan_output(raw)
    assert entries == [{"device": "/dev/sdz", "type": "sat"},
                       {"device": "/dev/sdy", "type": ""}]


def test_scan_devices_prefers_json(monkeypatch) -> None:
    """scan_devices 首选 --scan-open -j（真实打开、附 open_error 可跳）。"""
    monkeypatch.setattr(smart, "_SCAN_CACHE", None)
    calls: list[list[str]] = []

    def fake(cmd: list[str]):
        calls.append(cmd)
        return _proc(SCAN_JSON, rc=0)

    monkeypatch.setattr(smart, "_run_cmd", fake)
    entries = smart.scan_devices()
    assert [e["device"] for e in entries] == ["/dev/sda", "/dev/sdb"]
    assert calls[0][:2] == ["smartctl", "--scan-open"]
    assert len(calls) == 1  # 第一个变体成功即不再回退


def test_scan_open_skips_open_error_entries(monkeypatch) -> None:
    """--scan-open 里带 open_error 的设备（权限/休眠）跳过，不进映射。"""
    raw = json.dumps({"devices": [
        {"name": "/dev/sda", "type": "nvme"},
        {"name": "/dev/sdc", "type": "sat",
         "open_error": "Access is denied"},
    ]})
    monkeypatch.setattr(smart, "_SCAN_CACHE", None)
    monkeypatch.setattr(smart, "_run_cmd",
                        lambda cmd: _proc(raw, rc=0))
    assert smart.scan_devices() == [{"device": "/dev/sda", "type": "nvme"}]


def test_parse_scan_output_empty_noise() -> None:
    assert smart.parse_scan_output("") == []
    assert smart.parse_scan_output("# comment\n\nsmartctl 7.2\n") == []


def test_exit_status_mask_usable() -> None:
    # 健康告警位（8/16/32/64/128）与"子命令失败"位(4)：有 JSON 就算读到数据
    for rc in (0, 4, 8, 16, 32, 64, 128, 8 | 16):
        assert smart._usable(_proc(SAMPLE, rc=rc)), rc
    # 致命位：bit0 命令行错误 / bit1 设备打不开 → 即使吐了 JSON 文档也算失败
    for rc in (1, 2, 3):
        assert not smart._usable(_proc(SAMPLE, rc=rc)), rc
    # 无 JSON 输出一律不算
    assert not smart._usable(_proc("error text", rc=8))
    assert not smart._usable(None)


@pytest.mark.parametrize("rc,health", [
    (0, "unavailable"),
    (1, "unavailable"),   # 命令行错误：读不到数据（health 由 _usable 拦下）
    (2, "unavailable"),
    (8, "failed"),        # 盘报 FAILING
    (16, "warning"),      # 预失败属性超阈值
    (32, "warning"),      # 历史告警
    (64, "warning"),
    (128, "warning"),
    (8 | 16, "failed"),   # FAILING 优先于 warning
])
def test_parse_smart_exit_status_health(rc: int, health: str) -> None:
    assert smart.parse_smart("{}", exit_status=rc)["health"] == health


def test_parse_smart_passed_overrides_bits() -> None:
    assert smart.parse_smart(SAMPLE, exit_status=16)["health"] == "passed"
    assert smart.parse_smart(SAMPLE_FAILED, exit_status=0)["health"] == "failed"


def test_parse_smart_physical_fields() -> None:
    """实机证据字段：TOSHIBA MQ04UBB400 / 16NDT0O1T / JS0B0U / 4TB / 5400rpm。"""
    raw = json.dumps({
        "device": {"name": "/dev/sdc", "type": "sat"},
        "model_name": "TOSHIBA MQ04UBB400",
        "serial_number": "16NDT0O1T",
        "firmware_version": "JS0B0U",
        "user_capacity": {"blocks": 7814037168, "bytes": 4000787030016},
        "rotation_rate": 5400,
        "form_factor": {"ata_value": 2, "name": "3.5 inches"},
        "smart_status": {"passed": True},
        "temperature": {"current": 27},
    })
    p = smart.parse_smart(raw)
    assert p["model"] == "TOSHIBA MQ04UBB400"
    assert p["serial"] == "16NDT0O1T"
    assert p["firmware"] == "JS0B0U"
    assert p["capacity_bytes"] == 4000787030016
    assert p["rotation_rate"] == 5400
    assert p["form_factor"] == "3.5 inches"


def test_parse_smart_device_model_fallback() -> None:
    """model_name 缺失时回退 device.model（桥/SCSI 盘形态）。"""
    raw = json.dumps({"device": {"type": "scsi", "model": "Bridge Model"}})
    assert smart.parse_smart(raw)["model"] == "Bridge Model"


def test_smart_device_scan_mapping(monkeypatch) -> None:
    """scan 输出顺序与 disk_index 一致：index=2 → /dev/sdc + 建议类型 sat。"""
    monkeypatch.setattr(smart, "_SCAN_CACHE", [
        {"device": "/dev/sda", "type": "ata"},
        {"device": "/dev/sdb", "type": "scsi"},
        {"device": "/dev/sdc", "type": "sat"},
    ])
    assert smart.smart_device(2) == ("/dev/sdc", "sat")
    assert smart.smart_device(0) == ("/dev/sda", "ata")
    # 超出 scan 范围 → 候选链回退（首选 /dev/sdN 形态）
    assert smart.smart_device(9) == ("/dev/sdj", "")


def test_smart_device_env_override(monkeypatch) -> None:
    monkeypatch.setenv("CLDM_SMARTCTL_DEVICE", "/dev/sdz")
    assert smart.smart_device(2) == ("/dev/sdz", "")


def test_smart_device_candidates_dedup_same_disk(monkeypatch) -> None:
    """候选只出 /dev/sdN 形态：\\\\.\\PhysicalDriveN 不是 smartctl 认可的
    设备名（裸传报 Invalid argument），调研定案一律不进候选链。"""
    monkeypatch.setattr(smart, "_SCAN_CACHE", [])
    monkeypatch.delenv("CLDM_SMARTCTL_DEVICE", raising=False)
    cands = smart.smart_device_candidates(2)
    assert [c["device"] for c in cands] == ["/dev/sdc"]
    assert cands[0]["source"] == "fallback-sd"
    assert "PhysicalDrive" in cands[0]["note"]
    # 首选仍向后兼容（smart_device 取第一候选）
    assert smart.smart_device(2) == ("/dev/sdc", "")
    # sd 形态给不出（≥26）→ 没有任何形态可用（pd 不合法，不兜底）
    assert smart.smart_device_candidates(30) == []


def test_smart_device_candidates_scan_first(monkeypatch) -> None:
    monkeypatch.setattr(smart, "_SCAN_CACHE", [
        {"device": "/dev/sda", "type": "ata"},
        {"device": "/dev/sdb", "type": "nvme"},
        {"device": "/dev/sdc", "type": "sat"},
    ])
    cands = smart.smart_device_candidates(2)
    assert cands[0]["device"] == "/dev/sdc" and cands[0]["type"] == "sat"
    # /dev/sdc 已在首选里，fallback-sd 去重，pd 形态不进候选链
    assert [c["device"] for c in cands] == ["/dev/sdc"]


def test_device_type_chain_order() -> None:
    """Linux: auto → sat → sat,12 → sat,auto → 扫描建议 → 桥专用；去重保序。"""
    chain = smart._device_types("sat")
    assert chain[0] == "" and chain[1] == "sat" and chain[2] == "sat,12"
    assert "sat" not in chain[3:]
    for b in smart._BRIDGE_TYPES:
        assert b in chain
    assert len(chain) == len(set(chain))
    # 未给扫描建议时 base + 桥
    assert smart._device_types() == ["", "sat", "sat,12", "sat,auto",
                                     "usbjmicron", "usbjmicron,p",
                                     "usbjmicron,x", "usbsunplus",
                                     "usbprolific"]


def test_bridge_types_no_raid_writers() -> None:
    """jms56x/jmb39x/sntjmicron 是 RAID 盒专用（会向 RAID 卷写 LBA，
    普通单盘有覆写风险），绝不进兜底链；usbjmicron 变体安全可用。"""
    for banned in ("jms56x,0", "jms56x", "jmb39x", "sntjmicron"):
        assert banned not in smart._BRIDGE_TYPES
        assert banned not in smart._device_types()
        assert banned not in smart._device_types(windows=True)
    for b in ("usbjmicron", "usbjmicron,p", "usbjmicron,x",
              "usbsunplus", "usbprolific"):
        assert b in smart._BRIDGE_TYPES


def test_scan_devices_does_not_cache_failure(monkeypatch) -> None:
    """空扫描结果（瞬时失败）不进缓存：第一次空 → 第二次有。"""
    monkeypatch.setattr(smart, "_SCAN_CACHE", None)
    state = {"n": 0}

    def fake(cmd: list[str]):
        state["n"] += 1
        if state["n"] <= 6:  # 第一次 scan_devices：两轮重试 × 3 个变体全空
            return _proc("")
        return _proc('/dev/sdb -d sat # [SAT], ATA device')

    monkeypatch.setattr(smart, "_run_cmd", fake)
    assert smart.scan_devices() == []
    assert smart._SCAN_CACHE is None  # 失败不污染缓存
    entries = smart.scan_devices()
    assert [e["device"] for e in entries] == ["/dev/sdb"]
    assert smart._SCAN_CACHE == entries  # 非空才缓存


def test_classify_attempts_bit1_beats_bit0() -> None:
    """跨尝试失败归类：bit1（设备打不开）优先于 bit0（参数错）。"""
    attempts = [
        {"exit_status": 1, "error": None, "stderr_excerpt": "", "stdout_messages": ""},
        {"exit_status": 2, "error": None, "stderr_excerpt": "", "stdout_messages": ""},
    ]
    assert smart._classify_attempts(attempts) == "device_open"
    # 纯参数错才报 cmdline_error
    assert smart._classify_attempts(
        [{"exit_status": 1, "error": None, "stderr_excerpt": "",
          "stdout_messages": ""}]) == "cmdline_error"


def test_device_candidates_for_path_linux(monkeypatch) -> None:
    """Linux：device_candidates_for_path 复用 /proc/mounts 定位出单候选。"""
    monkeypatch.setattr(smart, "device_for_path", lambda p, i=None: "/dev/sdb")
    cands = smart.device_candidates_for_path("/mnt/x")
    assert cands == [{"device": "/dev/sdb", "type": "", "source": "linux"}]
    # 定位失败 → 空列表（绝不抛）
    def boom(p, i=None):
        raise RuntimeError("no mount")
    monkeypatch.setattr(smart, "device_for_path", boom)
    assert smart.device_candidates_for_path("/mnt/x") == []


def test_device_candidates_for_path_with_disk_index(monkeypatch) -> None:
    """给了盘号直接走 smart_device_candidates 全链（同盘去重后只剩 sd 形态）。"""
    monkeypatch.setattr(smart, "_SCAN_CACHE", [])
    cands = smart.device_candidates_for_path("/mnt/x", disk_index=1)
    assert [c["device"] for c in cands] == ["/dev/sdb"]


def test_type_fallback_chain_commands(monkeypatch) -> None:
    """每个候选类型各发一次 smartctl；-d 值/顺序、设备串位置正确。"""
    fake, calls = _fake_cmd_ex([_proc("", rc=2)])
    monkeypatch.setattr(smart, "_run_cmd_ex", fake)
    res = smart.read_smart_verbose("/dev/sdc")
    assert res["ok"] is False
    types = smart._device_types()
    assert [a["device_type"] for a in res["attempts"]] == \
        [("default" if t == "" else t) for t in types]
    for cmd, t in zip(calls, types):
        assert cmd[-1] == "/dev/sdc"
        if t:
            assert cmd[cmd.index("-d") + 1] == t
            assert cmd.index("-d") < cmd.index("-i")  # -d 靠前（smartctl 要求）
        else:
            assert "-d" not in cmd


def test_read_smart_chain_args_and_exit_status(monkeypatch) -> None:
    """CLDM_SMARTCTL_ARGS 与兜底链叠加；成功结果带 exit_status。"""
    fake, calls = _fake_cmd([_proc("", rc=2), _proc(SAMPLE, rc=0)])
    monkeypatch.setattr(smart, "_run_cmd", fake)
    monkeypatch.setenv("CLDM_SMARTCTL_ARGS", "--nocheck standby")
    res = smart.read_smart("/dev/sdc")
    assert res is not None and res["device_type"] == "sat"
    assert res["exit_status"] == 0
    assert calls[1][:5] == ["smartctl", "--nocheck", "standby", "-d", "sat"]


# ---------------------------------------------------------------- 采集失败原因落 meta


def test_collect_smart_error_meta(tmp_path: Path, monkeypatch) -> None:
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    data_root = tmp_path / "data"
    vol = VolumeInfo(
        filesystem="ext4", label="L", volume_serial_hex="abcd-1234",
        partition_uuid="1111-2222", partition_index=1, partition_table_type="GPT",
        capacity_bytes=1_000, free_bytes=900, mount_point=str(scan_root),
        device_path="/dev/sdb1",
    )
    disk = DiskInfo(
        disk_serial="SER123", serial_source="probe",
        smart_status="unavailable",
        smart_error=smart.REASON_MESSAGES["permission_denied"],
        smart_error_raw="Open failed: Access is denied",
    )

    monkeypatch.setattr(
        "cold_manifest.collect.probe_path",
        lambda path, *, manual_serial=None, smartctl=True: (vol, disk))

    result = collect_volume(scan_root, data_root=data_root)
    conn = sqlite3.connect(result.db_path)
    conn.row_factory = sqlite3.Row
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    conn.close()
    assert meta["smart_status"] == "unavailable"
    assert "管理员" in meta["smart_error"]
    assert meta["smart_error_raw"] == "Open failed: Access is denied"
    assert "smart_raw_json" not in meta

    cat = connect_catalog(data_root)
    assert smart.list_smart(cat, "SER123") == []
    cat.close()


def test_collect_smart_attempts_meta(tmp_path: Path, monkeypatch) -> None:
    """尝试链落 meta（smart_attempts_json）：失败诊断的核心证据。"""
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    data_root = tmp_path / "data"
    vol = VolumeInfo(
        filesystem="ext4", label="L", volume_serial_hex="abcd-1234",
        partition_uuid="1111-2222", partition_index=1, partition_table_type="GPT",
        capacity_bytes=1_000, free_bytes=900, mount_point=str(scan_root),
        device_path="/dev/sdb1",
    )
    attempts = [
        {"device_type": "default", "argv": "smartctl -i -H -A -j /dev/sdc",
         "rc": 2, "exit_status": 2, "error": None, "ok": False,
         "stderr_excerpt": "Unable to detect device type"},
        {"device_type": "sat", "argv": "smartctl -d sat -i -H -A -j /dev/sdc",
         "rc": 1, "exit_status": 1, "error": None, "ok": False,
         "stderr_excerpt": "Invalid argument"},
    ]
    disk = DiskInfo(
        disk_serial="SER123", serial_source="probe",
        smart_status="unavailable",
        smart_error=smart.REASON_MESSAGES["device_type_unknown"],
        smart_error_raw="Invalid argument",
        smart_attempts=attempts,
    )
    monkeypatch.setattr(
        "cold_manifest.collect.probe_path",
        lambda path, *, manual_serial=None, smartctl=True: (vol, disk))

    result = collect_volume(scan_root, data_root=data_root)
    conn = sqlite3.connect(result.db_path)
    conn.row_factory = sqlite3.Row
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    conn.close()
    assert "smart_raw_json" not in meta
    recorded = json.loads(meta["smart_attempts_json"])
    assert len(recorded) == 2
    assert recorded[1]["device_type"] == "sat"
    assert recorded[1]["exit_status"] == 1
    assert recorded[1]["argv"].endswith("/dev/sdc")


# ---------------------------------------------------------------- disk_smart 历史


def _mk_catalog(tmp_path: Path):
    data_root = tmp_path / "data"
    data_root.mkdir()
    conn = connect_catalog(data_root)
    ensure_disk(conn, "D1", physical_model="M")
    ensure_volume(conn, "vol", "D1", filesystem="ext4")
    register_snapshot(conn, "vol/20260101T000000Z", "vol", status="sealed",
                      host_path="/x", collected_at="2026-01-01T00:00:00Z")
    conn.commit()
    return data_root, conn


def _mk_snapshot_db(data_root: Path, meta_raw: "str | None") -> None:
    d = data_root / "vol" / "20260101T000000Z"
    d.mkdir(parents=True)
    conn = sqlite3.connect(d / "snapshot.db")
    init_snapshot(conn)
    if meta_raw is not None:
        conn.execute("INSERT INTO meta(key, value) VALUES('smart_raw_json', ?)", (meta_raw,))
    conn.commit()
    conn.close()


def test_record_and_list(tmp_path: Path) -> None:
    _data_root, conn = _mk_catalog(tmp_path)
    smart.record_smart(conn, "D1", "vol/20260101T000000Z", smart.parse_smart(SAMPLE),
                       collected_at="2026-01-01T00:00:00Z", raw_json=SAMPLE)
    smart.record_smart(conn, "D1", "vol/20260101T000000Z", smart.parse_smart(SAMPLE),
                       collected_at="2026-01-01T00:00:00Z", raw_json=SAMPLE)  # 幂等覆盖
    conn.commit()
    hist = smart.list_smart(conn, "D1")
    assert len(hist) == 1
    assert hist[0]["health"] == "passed" and hist[0]["temperature_c"] == 31
    conn.close()


def test_record_missing_smart_idempotent(tmp_path: Path) -> None:
    data_root, conn = _mk_catalog(tmp_path)
    _mk_snapshot_db(data_root, SAMPLE)
    assert smart.record_missing_smart(conn, data_root) == 1
    assert smart.record_missing_smart(conn, data_root) == 0  # 幂等
    hist = smart.list_smart(conn, "D1")
    assert len(hist) == 1
    assert hist[0]["collected_at"] == "2026-01-01T00:00:00Z"
    conn.close()


def test_record_missing_smart_skips_no_meta(tmp_path: Path) -> None:
    data_root, conn = _mk_catalog(tmp_path)
    _mk_snapshot_db(data_root, None)
    assert smart.record_missing_smart(conn, data_root) == 0
    assert smart.list_smart(conn, "D1") == []
    conn.close()


# ---------------------------------------------------------------- 采集链路


def _fake_probe_with_smart(mount: str, raw: "str | None"):
    vol = VolumeInfo(
        filesystem="ext4", label="L", volume_serial_hex="abcd-1234",
        partition_uuid="1111-2222", partition_index=1, partition_table_type="GPT",
        capacity_bytes=1_000, free_bytes=900, mount_point=mount,
        device_path="/dev/sdb1",
    )
    disk = DiskInfo(
        physical_model="TOSHIBA HDWG480", physical_serial="SER123",
        disk_serial="SER123", serial_source="smartctl",
        smart_status=smart.parse_smart(raw)["health"] if raw else "unavailable",
        smart_raw=raw,
        smart_device_type="sat" if raw else "",
    )

    def probe(path, *, manual_serial=None, smartctl=True):
        return vol, disk

    return probe


def _make_tree(root: Path) -> None:
    (root / "a").mkdir(parents=True)
    (root / "a" / "x.txt").write_bytes(b"x" * 10)


@pytest.mark.parametrize("raw,expect_row", [(SAMPLE, True), (None, False)])
def test_collect_smart_linkage(tmp_path: Path, monkeypatch,
                               raw: "str | None", expect_row: bool) -> None:
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    data_root = tmp_path / "data"
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _fake_probe_with_smart(str(scan_root), raw))

    result = collect_volume(scan_root, data_root=data_root)
    conn = sqlite3.connect(result.db_path)
    conn.row_factory = sqlite3.Row
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    conn.close()
    assert meta.get("smart_raw_json") == raw

    cat = connect_catalog(data_root)
    rows = smart.list_smart(cat, "SER123")
    if expect_row:
        assert len(rows) == 1
        assert rows[0]["snapshot_id"] == result.snapshot_id
        assert rows[0]["health"] == "passed"
        assert rows[0]["device_type"] == "sat"  # SAMPLE 的 device.type
        assert rows[0]["raw_json"] == SAMPLE
        # meta 结构化字段
        assert meta["smart_health"] == "passed"
        assert meta["smart_temperature_c"] == "31"
        assert meta["smart_power_on_hours"] == "12345"
        assert meta["smart_reallocated_ct"] == "8"
    else:
        assert rows == []
        assert "smart_temperature_c" not in meta
        assert meta["smart_status"] == "unavailable"
    cat.close()


# ---------------------------------------------------------------- SSD 字段（P4-②扩展）

# 用户实机真实 NVMe JSON（WD Blue SN570 2TB，smartctl -i -H -A -j /dev/sdb）
NVME_SAMPLE = json.dumps({
    "device": {"name": "/dev/sdb", "type": "nvme", "protocol": "NVMe"},
    "model_name": "WD Blue SN570 2TB SSD",
    "serial_number": "23024Q800919",
    "smart_status": {"passed": True},
    "temperature": {"current": 60},
    "nvme_smart_health_information_log": {
        "temperature": 60, "available_spare": 100,
        "available_spare_threshold": 10, "percentage_used": 0,
        "data_units_read": 15578192,
        "data_units_written": 17379040,
        "host_reads": 47631956, "host_writes": 92383099,
        "controller_busy_time": 174, "power_cycles": 308,
        "power_on_hours": 923, "unsafe_shutdowns": 22,
        "media_errors": 0, "num_err_log_entries": 0,
        "warning_temp_time": 1, "critical_comp_time": 0,
        "temperature_sensors": [82, 52],
    },
})

# SATA SSD 属性表夹具（属性 ID 口径）
SATA_SSD_SAMPLE = json.dumps({
    "device": {"name": "/dev/sdc", "type": "sat", "protocol": "ATA"},
    "model_name": "Kingston SA400S37240G",
    "serial_number": "50026B77820ABCDE",
    "smart_status": {"passed": True},
    "temperature": {"current": 32},
    "rotation_rate": 0,
    "ata_smart_attributes": {"table": [
        {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 0, "string": "0"}},
        {"id": 9, "name": "Power_On_Hours", "raw": {"value": 9000, "string": "9000"}},
        {"id": 177, "name": "Wear_Leveling_Count", "raw": {"value": 91, "string": "91"}},
        {"id": 202, "name": "Percent_Lifetime_Remain", "raw": {"value": 9, "string": "9"}},
        {"id": 231, "name": "SSD_Life_Left", "raw": {"value": 91, "string": "91"}},
        {"id": 241, "name": "Total_LBAs_Written", "raw": {"value": 117440512,
                                                          "string": "117440512"}},
        {"id": 242, "name": "Total_LBAs_Read", "raw": {"value": 234881024,
                                                       "string": "234881024"}},
    ]},
})


def test_parse_smart_nvme_ssd_fields() -> None:
    p = smart.parse_smart(NVME_SAMPLE)
    ssd = p["ssd"]
    assert ssd["source"] == "nvme"
    assert ssd["percentage_used"] == 0
    assert ssd["life_left_pct"] == 100
    assert ssd["available_spare_pct"] == 100
    assert ssd["available_spare_threshold"] == 10
    # 17379040 units × 1000 × 512B ≈ 8.9 TB
    assert ssd["written_bytes"] == 17379040 * 1000 * 512
    assert abs(ssd["written_bytes"] - 8.9e12) < 0.01e12
    assert ssd["written_tb"] == round(17379040 * 512000 / 1e12, 2)
    assert ssd["read_bytes"] == 15578192 * 1000 * 512
    assert ssd["host_reads"] == 47631956 and ssd["host_writes"] == 92383099
    assert ssd["media_errors"] == 0
    assert ssd["unsafe_shutdowns"] == 22
    assert ssd["power_cycles"] == 308
    assert ssd["controller_busy_minutes"] == 174
    assert ssd["temp_sensors"] == [82, 52]
    assert ssd["num_err_log_entries"] == 0
    assert ssd["warning_temp_time"] == 1 and ssd["critical_comp_time"] == 0


def test_parse_smart_sata_ssd_fields() -> None:
    p = smart.parse_smart(SATA_SSD_SAMPLE)
    ssd = p["ssd"]
    assert ssd["source"] == "ata"
    assert ssd["life_left_pct"] == 91          # 231 优先
    assert ssd["written_bytes"] == 117440512 * 512
    assert ssd["read_bytes"] == 234881024 * 512
    assert ssd["wear_leveling_count"] == 91
    assert p["power_on_hours"] == 9000
    assert p["health"] == "passed"


def test_parse_smart_sata_life_attr_priority() -> None:
    """231 缺 → 233；233 缺 → 202；255（未知）不进 life_left_pct。"""
    raw = json.dumps({"ata_smart_attributes": {"table": [
        {"id": 233, "name": "Media_Wearout_Indicator", "raw": {"value": 87}},
        {"id": 202, "name": "Percent_Lifetime_Remain", "raw": {"value": 13}},
    ]}})
    assert smart.parse_smart(raw)["ssd"]["life_left_pct"] == 87
    raw = json.dumps({"ata_smart_attributes": {"table": [
        {"id": 202, "name": "Percent_Lifetime_Remain", "raw": {"value": 13}},
    ]}})
    assert smart.parse_smart(raw)["ssd"]["life_left_pct"] == 13
    raw = json.dumps({"ata_smart_attributes": {"table": [
        {"id": 231, "name": "SSD_Life_Left", "raw": {"value": 255}},
    ]}})
    assert smart.parse_smart(raw)["ssd"].get("life_left_pct") is None


def test_parse_smart_hdd_no_ssd() -> None:
    """HDD（SAMPLE 无任何 SSD 属性/NVMe 段）→ ssd 为空字典。"""
    p = smart.parse_smart(SAMPLE)
    assert p["ssd"] == {}
    assert smart.parse_smart("{}")["ssd"] == {}
    assert smart.parse_smart(None)["ssd"] == {}
    assert smart.parse_smart("garbage")["ssd"] == {}


def test_ssd_contract_frozen_keys() -> None:
    c = smart.ssd_contract(smart.parse_smart(NVME_SAMPLE)["ssd"])
    assert set(c.keys()) == set(smart.SSD_CONTRACT_KEYS)
    assert c["life_left_pct"] == 100
    assert c["written_bytes"] == 17379040 * 1000 * 512
    assert c["written_tb"] == round(17379040 * 512000 / 1e12, 2)
    assert c["temp_sensors"] == [82, 52]
    assert c["source"] == "nvme"
    # 缺失兜底：全 null / []
    c = smart.ssd_contract({})
    assert set(c.keys()) == set(smart.SSD_CONTRACT_KEYS)
    assert c["temp_sensors"] == []
    assert all(c[k] is None for k in smart.SSD_CONTRACT_KEYS if k not in
               ("temp_sensors",))


def test_disk_smart_ssd_columns_roundtrip(tmp_path: Path) -> None:
    data_root, conn = _mk_catalog(tmp_path)
    for raw in (NVME_SAMPLE, SAMPLE):
        smart.record_smart(conn, "D1", f"vol/2026010{2 if raw == SAMPLE else 1}T000000Z",
                           smart.parse_smart(raw), raw_json=raw)
    conn.commit()
    hist = smart.list_smart(conn, "D1")
    assert len(hist) == 2
    nvme_row = next(r for r in hist if r["raw_json"] == NVME_SAMPLE)
    assert nvme_row["life_left_pct"] == 100
    assert nvme_row["percentage_used"] == 0
    assert nvme_row["written_bytes"] == 17379040 * 1000 * 512
    assert nvme_row["read_bytes"] == 15578192 * 1000 * 512
    assert nvme_row["media_errors"] == 0
    assert nvme_row["unsafe_shutdowns"] == 22
    assert nvme_row["power_cycles"] == 308
    assert nvme_row["controller_busy_minutes"] == 174
    assert json.loads(nvme_row["ssd_json"])["source"] == "nvme"
    hdd_row = next(r for r in hist if r["raw_json"] == SAMPLE)
    assert hdd_row["life_left_pct"] is None and hdd_row["ssd_json"] is None
    conn.close()


def test_catalog_migration_adds_ssd_columns_idempotent(tmp_path: Path) -> None:
    """旧库（无 SSD 列）init_catalog 补列且幂等，旧行缺列为 NULL。"""
    import sqlite3 as _sq

    from cold_manifest.db import init_catalog
    from cold_manifest.schema import CATALOG_DDL

    data_root = tmp_path / "data"
    data_root.mkdir()
    db = data_root / "catalog.db"
    # 手工建一个"旧版"库：把 SSD 列声明从 DDL 里裁掉
    import re

    m = re.search(r"(CREATE TABLE IF NOT EXISTS disk_smart.*?)\n\);", CATALOG_DDL, re.S)
    disk_smart_old = re.sub(
        r"\n  -- SSD 专属.*?\n", "\n", m.group(1), flags=re.S) + "\n);"
    old_ddl = CATALOG_DDL[:m.start()] + disk_smart_old + CATALOG_DDL[m.end():]
    conn = _sq.connect(db)
    conn.row_factory = _sq.Row
    conn.executescript(old_ddl)
    conn.execute("INSERT INTO disk_smart(disk_id, snapshot_id, health)"
                 " VALUES('D1', 's1', 'passed')")
    conn.commit()
    init_catalog(conn)  # 第一次迁移
    init_catalog(conn)  # 幂等
    cols = {r[1] for r in conn.execute("PRAGMA table_info(disk_smart)")}
    for c in ("life_left_pct", "percentage_used", "available_spare_pct",
              "written_bytes", "read_bytes", "media_errors",
              "unsafe_shutdowns", "power_cycles", "controller_busy_minutes",
              "ssd_json"):
        assert c in cols
    row = conn.execute("SELECT * FROM disk_smart").fetchone()
    assert row["health"] == "passed" and row["ssd_json"] is None
    conn.close()


def test_collect_smart_ssd_meta(tmp_path: Path, monkeypatch) -> None:
    """采集链路：SSD 字段落 meta smart_ssd_json + disk_smart 列。"""
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    data_root = tmp_path / "data"
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _fake_probe_with_smart(str(scan_root), NVME_SAMPLE))
    result = collect_volume(scan_root, data_root=data_root)
    conn = sqlite3.connect(result.db_path)
    conn.row_factory = sqlite3.Row
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    conn.close()
    ssd_meta = json.loads(meta["smart_ssd_json"])
    assert ssd_meta["source"] == "nvme"
    assert ssd_meta["written_bytes"] == 17379040 * 1000 * 512

    cat = connect_catalog(data_root)
    rows = smart.list_smart(cat, "SER123")
    assert len(rows) == 1
    assert rows[0]["life_left_pct"] == 100
    assert rows[0]["written_bytes"] == 17379040 * 1000 * 512
    cat.close()


def test_snapshot_report_shows_ssd(tmp_path: Path) -> None:
    """快照 HTML 报告：meta smart_ssd_json → SMART 行带剩余寿命/累计写入。"""
    import io

    from cold_manifest.db import init_snapshot
    from cold_manifest.report import generate_snapshot_report

    db = tmp_path / "snapshot.db"
    conn = sqlite3.connect(db)
    init_snapshot(conn)
    ssd = smart.parse_smart(NVME_SAMPLE)["ssd"]
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES(?, ?)",
        [("status", "sealed"), ("volume_id", "vol"),
         ("smart_health", "passed"),
         ("smart_ssd_json", json.dumps(ssd, ensure_ascii=False))])
    conn.commit()
    buf = io.StringIO()
    generate_snapshot_report(conn, buf, snapshot_id="vol/x")
    html = buf.getvalue()
    assert "剩余寿命 100%" in html
    assert "累计写入" in html and "TB" in html
    conn.close()


# ---------------------------------------------------------------- ATA 属性表/关键指标

# 真实 ATA JSON 子集夹具（字段完整、值真实；TOSHIBA MQ04UBB400 USB 桥 -d sat 形态）
ATA_FULL = json.dumps({
    "device": {"name": "/dev/sdc", "type": "sat", "protocol": "ATA"},
    "model_family": "Toshiba MQ04UBB series",
    "model_name": "TOSHIBA MQ04UBB400",
    "serial_number": "  Y7BPT04XT  ",
    "firmware_version": "JU000U",
    "user_capacity": {"blocks": 7814037168, "bytes": 4000787030016},
    "rotation_rate": 5400,
    "form_factor": {"ata_value": 3, "name": "2.5 inches"},
    "sata_version": {"string": "SATA 3.3", "value": 510},
    "ata_version": {"string": "ACS-3 (minor revision not indicated)", "value": 1027},
    "interface_speed": {"max": {"string": "6.0 Gb/s", "unit": "Gb/s"},
                        "current": {"string": "6.0 Gb/s", "unit": "Gb/s"}},
    "trim": {"supported": False},
    "zoned_device": {"name": "not_zoned", "value": 0},
    "smart_status": {"passed": True},
    "temperature": {"current": 29},
    "power_on_time": {"hours": 1189},
    "ata_smart_attributes": {"table": [
        {"id": 1, "name": "Raw_Read_Error_Rate", "value": 100, "worst": 100,
         "thresh": 50, "raw": {"value": 0, "string": "0"},
         "when_failed": ""},
        {"id": 3, "name": "Spin_Up_Time", "value": 133, "worst": 126,
         "thresh": 23, "raw": {"value": 5625, "string": "5625"},
         "when_failed": ""},
        {"id": 4, "name": "Start_Stop_Count", "value": 96, "worst": 96,
         "thresh": 0, "raw": {"value": 4673, "string": "4673"},
         "when_failed": ""},
        {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "worst": 100,
         "thresh": 50, "raw": {"value": 0, "string": "0"}, "when_failed": ""},
        {"id": 7, "name": "Seek_Error_Rate", "value": 100, "worst": 100,
         "thresh": 50, "raw": {"value": 0, "string": "0"}, "when_failed": ""},
        {"id": 9, "name": "Power_On_Hours", "value": 85, "worst": 85,
         "thresh": 0, "raw": {"value": 1189, "string": "1189"},
         "when_failed": ""},
        {"id": 10, "name": "Spin_Retry_Count", "value": 100, "worst": 100,
         "thresh": 30, "raw": {"value": 0, "string": "0"}, "when_failed": ""},
        {"id": 12, "name": "Power_Cycle_Count", "value": 96, "worst": 96,
         "thresh": 0, "raw": {"value": 4627, "string": "4627"},
         "when_failed": ""},
        {"id": 190, "name": "Airflow_Temperature_Cel", "value": 66, "worst": 51,
         "thresh": 0, "raw": {"value": 34, "string": "34"}, "when_failed": ""},
        {"id": 192, "name": "Power-Off_Retract_Count", "value": 100, "worst": 100,
         "thresh": 0, "raw": {"value": 88, "string": "88"}, "when_failed": ""},
        {"id": 193, "name": "Load_Cycle_Count", "value": 96, "worst": 96,
         "thresh": 0, "raw": {"value": 4673, "string": "4673"},
         "when_failed": ""},
        {"id": 194, "name": "Temperature_Celsius", "value": 34, "worst": 49,
         "thresh": 0, "raw": {"value": 29, "string": "29"}, "when_failed": ""},
        {"id": 197, "name": "Current_Pending_Sector", "value": 100, "worst": 100,
         "thresh": 0, "raw": {"value": 0, "string": "0"}, "when_failed": ""},
        {"id": 199, "name": "UDMA_CRC_Error_Count", "value": 200, "worst": 200,
         "thresh": 0, "raw": {"value": 0, "string": "0"}, "when_failed": ""},
        # 48-bit 多字属性：raw.string 是 smartctl 拼好的完整十进制串
        {"id": 240, "name": "Head_Flying_Hours", "value": 100, "worst": 100,
         "thresh": 0, "raw": {"value": 1157774408, "string": "1157774408"},
         "when_failed": ""},
    ]},
})


def test_parse_smart_ata_attributes() -> None:
    p = smart.parse_smart(ATA_FULL)
    attrs = p["ata_attributes"]
    assert len(attrs) == 15
    first = attrs[0]
    assert first["id"] == 1
    assert first["name"] == "Raw_Read_Error_Rate"
    assert first["value"] == 100 and first["worst"] == 100 and first["thresh"] == 50
    assert first["raw_value"] == 0 and first["raw_string"] == "0"
    assert first["when_failed"] == ""


def test_parse_smart_hdd_metrics_and_identity() -> None:
    p = smart.parse_smart(ATA_FULL)
    assert p["power_cycle_count"] == 4627
    assert p["load_cycle_count"] == 4673
    assert p["udma_crc_errors"] == 0
    assert p["raw_read_error_rate"] == 0
    assert p["seek_error_rate"] == 0
    assert p["spin_retry_count"] == 0
    assert p["power_off_retract_count"] == 88
    assert p["airflow_temperature_c"] == 34
    assert p["head_flying_hours"] == 1157774408  # 48-bit 多字取 raw.string
    # 身份/链路细节
    assert p["interface_speed_current"] == "6.0 Gb/s"
    assert p["interface_speed_max"] == "6.0 Gb/s"
    assert p["sata_version"] == "SATA 3.3"
    assert p["ata_version"].startswith("ACS-3")
    assert p["trim"] is False
    assert p["zoned"] == "not_zoned"
    assert p["model_family"] == "Toshiba MQ04UBB series"
    assert p["capacity_bytes"] == 4000787030016
    assert p["rotation_rate"] == 5400
    assert p["form_factor"] == "2.5 inches"
    # temperature.current 优先于 attr 194
    assert p["temperature_c"] == 29


def test_parse_smart_temperature_fallback_attr194() -> None:
    raw = json.dumps({"ata_smart_attributes": {"table": [
        {"id": 194, "raw": {"value": 33}}]}})
    assert smart.parse_smart(raw)["temperature_c"] == 33


def test_parse_smart_nvme_ata_attributes_empty() -> None:
    p = smart.parse_smart(NVME_SAMPLE)
    assert p["ata_attributes"] == []
    for k in smart.ATA_CONTRACT_KEYS:
        if k == "trim":
            assert p[k] is False
        else:
            assert p[k] is None


def test_parse_smart_ata_bad_raw_never_raises() -> None:
    p = smart.parse_smart(json.dumps({"ata_smart_attributes": {"table": [
        {"id": 5, "raw": {"value": "x", "string": "n/a"}},
        {"id": 12},  # 无 raw
        "junk",
    ]}}))
    attrs = p["ata_attributes"]
    assert attrs[0]["raw_value"] is None
    assert attrs[0]["raw_string"] == "n/a"
    assert attrs[1]["raw_value"] is None
    assert len(attrs) == 2
    assert p["power_cycle_count"] is None


def test_ata_record_and_list_roundtrip(tmp_path: Path) -> None:
    cat = connect_catalog(tmp_path / "catalog.db")
    p = smart.parse_smart(ATA_FULL)
    smart.record_smart(cat, "D1", "vol/s", p)
    rows = smart.list_smart(cat, "D1")
    assert len(rows) == 1
    r = rows[0]
    assert r["power_cycle_count"] == 4627
    assert r["udma_crc_errors"] == 0
    assert r["sata_version"] == "SATA 3.3"
    assert r["trim"] == 0  # bool → int
    assert r["model_family"] == "Toshiba MQ04UBB series"
    attrs = json.loads(r["ata_json"])
    assert len(attrs) == 15 and attrs[0]["id"] == 1
    # 契约视图：历史行 → ata_contract
    c = smart.ata_contract(r)
    assert c["power_cycle_count"] == 4627
    assert len(c["ata_attributes"]) == 15
    assert c["trim"] is False
    cat.close()


def test_disk_smart_migration_idempotent(tmp_path: Path) -> None:
    from cold_manifest.db import init_catalog

    cat = sqlite3.connect(tmp_path / "catalog.db")
    init_catalog(cat)
    init_catalog(cat)  # 二次幂等
    cols = {r[1] for r in cat.execute("PRAGMA table_info(disk_smart)")}
    for name, _decl in smart.ATA_DB_COLUMNS:
        assert name in cols
    cat.close()


def test_snapshot_report_ata_metrics(tmp_path: Path) -> None:
    """快照 HTML 报告：SMART 关键指标表 + ATA 属性表（前 20 行 + 截断提示）。"""
    import io

    from cold_manifest.db import init_snapshot
    from cold_manifest.report import generate_snapshot_report

    db = tmp_path / "snapshot.db"
    conn = sqlite3.connect(db)
    init_snapshot(conn)
    conn.execute("INSERT INTO meta(key, value) VALUES('status','sealed')")
    conn.commit()
    p = smart.parse_smart(ATA_FULL)
    # 属性 >20 行以验证截断提示：整表复制两份（id 重复仅测试用）
    sm = dict(p)
    sm["ata_json"] = json.dumps(
        p["ata_attributes"] + p["ata_attributes"], ensure_ascii=False)
    buf = io.StringIO()
    generate_snapshot_report(conn, buf, snapshot_id="vol/x", smart_row=sm)
    html = buf.getvalue()
    assert "SMART 关键指标" in html
    assert "通电次数" in html and "4627" in html
    assert "CRC 错误" in html
    assert "转速" in html and "5400rpm" in html
    assert "SMART 属性表" in html
    assert "Raw_Read_Error_Rate" in html
    assert "其余 10 项见页面" in html
    conn.close()


# ---- 中文名（name_zh / when_failed_zh）与 HTML 报告中文渲染 ----


def test_ata_attr_names_covers_required_ids() -> None:
    for i in (1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 22, 177, 183, 184,
              187, 188, 189, 190, 191, 192, 193, 194, 195, 196, 197, 198,
              199, 200, 202, 206, 231, 233, 240, 241, 242):
        assert smart.ATA_ATTR_NAMES.get(i), f"缺属性 {i} 的中文名"
    assert smart.ATA_ATTR_NAMES[5] == "重分配扇区计数"
    assert smart.ATA_ATTR_NAMES[9] == "通电小时数"
    assert smart.ATA_ATTR_NAMES[12] == "通电次数"
    assert smart.ATA_ATTR_NAMES[190] == "气流温度"
    assert smart.ATA_ATTR_NAMES[194] == "温度"
    assert smart.ATA_ATTR_NAMES[197] == "当前待映射扇区数"
    assert smart.ATA_ATTR_NAMES[198] == "离线不可修正扇区数"
    assert smart.ATA_ATTR_NAMES[199] == "UDMA CRC 错误"
    assert smart.ATA_ATTR_NAMES[240] == "磁头飞行小时"
    assert smart.ATA_ATTR_NAMES[241] == "累计写入量"
    assert smart.ATA_ATTR_NAMES[242] == "累计读取量"


def test_ata_attributes_name_zh() -> None:
    p = smart.parse_smart(ATA_FULL)
    by_id = {a["id"]: a for a in p["ata_attributes"]}
    assert by_id[5]["name_zh"] == smart.ATA_ATTR_NAMES[5]
    assert by_id[5]["name"] == "Reallocated_Sector_Ct"  # 原英文不丢
    assert by_id[194]["name_zh"] == "温度"
    assert by_id[199]["name_zh"] == "UDMA CRC 错误"
    assert by_id[240]["name_zh"] == "磁头飞行小时"


def test_ata_attributes_unknown_id_name_zh_empty() -> None:
    raw = json.dumps({"ata_smart_attributes": {"table": [
        {"id": 99, "name": "Free_Fall_Sensor", "value": 100,
         "worst": 100, "thresh": 0, "raw": {"value": 0, "string": "0"},
         "when_failed": ""},
    ]}})
    attrs = smart.parse_smart(raw)["ata_attributes"]
    assert attrs[0]["id"] == 99
    assert attrs[0]["name"] == "Free_Fall_Sensor"
    assert attrs[0]["name_zh"] == ""


def test_when_failed_zh_mapping() -> None:
    raw = json.dumps({"ata_smart_attributes": {"table": [
        {"id": 5, "name": "Reallocated_Sector_Ct", "value": 100,
         "worst": 100, "thresh": 50, "raw": {"value": 0, "string": "0"},
         "when_failed": "FAILING_NOW"},
        {"id": 197, "name": "Current_Pending_Sector", "value": 100,
         "worst": 100, "thresh": 0, "raw": {"value": 0, "string": "0"},
         "when_failed": "In_the_past"},
        {"id": 199, "name": "UDMA_CRC_Error_Count", "value": 200,
         "worst": 200, "thresh": 0, "raw": {"value": 0, "string": "0"},
         "when_failed": ""},
        {"id": 250, "name": "Odd", "value": 100, "worst": 100,
         "thresh": 0, "raw": {"value": 0, "string": "0"},
         "when_failed": "Weird_Value"},
    ]}})
    by_id = {a["id"]: a for a in smart.parse_smart(raw)["ata_attributes"]}
    assert by_id[5]["when_failed"] == "FAILING_NOW"
    assert by_id[5]["when_failed_zh"] == "现在失败"
    assert by_id[197]["when_failed_zh"] == "曾经失败"
    assert by_id[199]["when_failed_zh"] == ""
    # 大小写不敏感
    assert smart._when_failed_zh("in_the_past") == "曾经失败"
    # 未知枚举原样返回；缺失 → None
    assert by_id[250]["when_failed_zh"] == "Weird_Value"
    assert smart._when_failed_zh(None) is None


def test_ata_contract_passes_name_zh() -> None:
    p = smart.parse_smart(ATA_FULL)
    c = smart.ata_contract(p)
    by_id = {a["id"]: a for a in c["ata_attributes"]}
    assert by_id[5]["name_zh"] == smart.ATA_ATTR_NAMES[5]
    assert by_id[5]["when_failed_zh"] == ""
    # 历史行（ata_json）→ 契约同样透出新键
    cat = connect_catalog(":memory:")
    smart.record_smart(cat, "D1", "v/s", p)
    r = smart.list_smart(cat, "D1")[0]
    c2 = smart.ata_contract(r)
    by_id2 = {a["id"]: a for a in c2["ata_attributes"]}
    assert by_id2[5]["name_zh"] == smart.ATA_ATTR_NAMES[5]
    cat.close()


def test_report_attr_table_zh_names() -> None:
    """HTML 报告属性表：名字列 '中文 (English)'，状态列中文优先。"""
    import io

    from cold_manifest.db import init_snapshot
    from cold_manifest.report import generate_snapshot_report

    conn = sqlite3.connect(":memory:")
    init_snapshot(conn)
    conn.execute("INSERT INTO meta(key, value) VALUES('status','sealed')")
    conn.commit()
    p = smart.parse_smart(ATA_FULL)
    attrs = json.loads(json.dumps(p["ata_attributes"]))
    attrs[0]["when_failed"] = "FAILING_NOW"
    attrs[0]["when_failed_zh"] = "现在失败"
    sm = dict(p)
    sm["ata_json"] = json.dumps(attrs, ensure_ascii=False)
    buf = io.StringIO()
    generate_snapshot_report(conn, buf, snapshot_id="v/s", smart_row=sm)
    html = buf.getvalue()
    assert "重分配扇区计数 (Reallocated_Sector_Ct)" in html
    assert "温度 (Temperature_Celsius)" in html
    assert "现在失败" in html
    conn.close()


def test_ata_contract_enriches_historical_rows():
    """历史行补齐：旧版采集的 ata_json 没有 name_zh/when_failed_zh，契约层按 ID
    现查中文（已有快照不重采也能显示中文），且**不修改**调用方传入的对象。"""
    from cold_manifest import smart

    old = [{"id": 5, "name": "Reallocated_Sector_Ct", "value": 100, "worst": 100,
            "thresh": 10, "raw_value": 0, "raw_string": "0", "when_failed": "FAILING_NOW"}]
    out = smart.ata_contract({"ata_json": json.dumps(old, ensure_ascii=False)})
    a = out["ata_attributes"][0]
    assert a["name_zh"] == smart.ATA_ATTR_NAMES[5] == "重分配扇区计数"
    assert a["when_failed_zh"] == "现在失败"
    assert "name_zh" not in old[0]  # 原对象未被修改

    unknown_id = next(i for i in range(200, 400) if i not in smart.ATA_ATTR_NAMES)
    out2 = smart.ata_contract({"ata_json": json.dumps(
        [{"id": unknown_id, "name": "Vendor_Specific"}], ensure_ascii=False)})
    assert out2["ata_attributes"][0]["name_zh"] == ""          # 未知 ID 回退英文
    assert out2["ata_attributes"][0]["name"] == "Vendor_Specific"
