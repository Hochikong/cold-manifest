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
        p = subprocess.CompletedProcess(args=[], returncode=2, stdout="",
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
    fake, _ = _fake_cmd_ex([_proc("", rc=2), _proc("", rc=2)])
    monkeypatch.setattr(smart, "_run_cmd_ex", fake)
    res = smart.read_smart_verbose("/dev/sdb")
    assert res["ok"] is False and res["reason"] == "other"
    assert res["message"]


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
    # 超出 scan 范围 → PhysicalDrive 回退
    assert smart.smart_device(9) == ("\\\\.\\PhysicalDrive9", "")


def test_smart_device_env_override(monkeypatch) -> None:
    monkeypatch.setenv("CLDM_SMARTCTL_DEVICE", "/dev/sdz")
    assert smart.smart_device(2) == ("/dev/sdz", "")


def test_smart_device_fallback_physicaldrive(monkeypatch) -> None:
    monkeypatch.setattr(smart, "_SCAN_CACHE", [])
    monkeypatch.delenv("CLDM_SMARTCTL_DEVICE", raising=False)
    assert smart.smart_device(2) == ("\\\\.\\PhysicalDrive2", "")


def test_device_type_chain_order() -> None:
    """auto → sat → sat,12 → 扫描建议 → 桥专用；去重保序。"""
    chain = smart._device_types("sat")
    assert chain[0] == "" and chain[1] == "sat" and chain[2] == "sat,12"
    assert "sat" not in chain[3:]
    for b in ("usbjmicron", "usbsunplus", "usbprolific", "jms56x"):
        assert b in chain
    assert len(chain) == len(set(chain))
    # 未给扫描建议时 base + 桥
    assert smart._device_types() == ["", "sat", "sat,12",
                                     "usbjmicron", "usbsunplus", "usbprolific", "jms56x"]


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
