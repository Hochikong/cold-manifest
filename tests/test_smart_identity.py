"""PR-B：SMART 设备按序列号校验身份（resolve_smart_device / probe 条件覆盖）。

背景：旧实现把 Win32_DiskDrive.Index 当 smartctl --scan 行序下标，全程无
序列号校验，同型号同容量多盘时会把另一块盘的 SMART（乃至序列号）写进
meta/catalog。本文件覆盖：身份打分排序、歧义检测、回退候选标记、probe
序列号覆盖的新语义（验证过才覆盖，否则只补全/保留 probe 值）。
"""

from __future__ import annotations

import json
import time

from cold_manifest import smart
from cold_manifest.probe import windows as pw

EXP = "SER-EXP"      # 期望（探测到的）序列号
OTHER = "SER-OTHER"


def _set_scan(monkeypatch, entries: list[dict]) -> None:
    monkeypatch.setattr(smart, "_SCAN_CACHE", entries)
    monkeypatch.delenv("CLDM_SMARTCTL_DEVICE", raising=False)


def _ident(serial: "str | None", model: "str | None" = "M",
           cap: "int | None" = 1000) -> "dict | None":
    return {"serial": serial, "capacity_bytes": cap, "model": model,
            "device_type": "sat", "raw": "{}"}


def _fake_idents(monkeypatch, by_device: dict) -> None:
    monkeypatch.setattr(smart, "read_device_identity",
                        lambda dev, sug=None: by_device.get(dev))


# ---------------------------------------------------------------- 打分


def test_score_serial_exact_and_mismatch() -> None:
    assert smart._score_identity_match(_ident(EXP), EXP, 1000, "M") == 100
    # 双方都有序列号但不一致、容量吻合 ≤5% → 90（expected 常是盒 ID 场景）
    assert smart._score_identity_match(_ident(OTHER), EXP, 1000, "M") == 90
    # 序列号不一致 **且** 容量差 >20% → 0（才判定是另一块盘）
    assert smart._score_identity_match(_ident(OTHER, cap=2000), EXP, 1000, "M") == 0
    # 序列号不一致、容量差 ≤20% 但 >5%、型号一致 → 50
    assert smart._score_identity_match(_ident(OTHER, cap=1100), EXP, 1000, "M") == 50
    # 容量吻合但型号明确不一致（盒型号 vs 真盘型号）→ 70（不致死，用于同容量区分）
    assert smart._score_identity_match(_ident(OTHER, model="N"), EXP, 1000, "M") == 70
    # serial 缺失：容量≤5% + 型号 → 80；型号明确不一致 → 70；型号不可比 → 20
    assert smart._score_identity_match(_ident(None), EXP, 1000, "M") == 80
    assert smart._score_identity_match(_ident(None, model="N"), EXP, 1000, "M") == 70
    assert smart._score_identity_match(_ident(None, model=None), EXP, 1000, None) == 20
    # 容量≤20% + 型号 → 50；仅容量 → 20；全无 → 0
    assert smart._score_identity_match(_ident(None, cap=1100), EXP, 1000, "M") == 50
    assert smart._score_identity_match(None, EXP, 1000, "M") == 0


# ---------------------------------------------------------------- 候选排序


def test_serial_match_beats_index_order(monkeypatch) -> None:
    """disk_index=0 但期望序列号属于条目 1 → 首选是条目 1。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"},
                            {"device": "/dev/sdb", "type": "sat"}])
    _fake_idents(monkeypatch, {"/dev/sda": _ident(OTHER),
                               "/dev/sdb": _ident(EXP)})
    cands, si = smart.resolve_smart_device(expected_serial=EXP, disk_index=0)
    assert cands[0]["device"] == "/dev/sdb"
    assert si["mapped_by"] == "serial_match"
    assert si.get("identity_ambiguity") is None
    # 读到不同序列号的 /dev/sda 被剔除
    assert [c["device"] for c in cands if c["source"] == "scan"] == ["/dev/sdb"]


def test_same_model_capacity_disks_each_match_own_serial(monkeypatch) -> None:
    """同型号同容量、serial 不同 → 各归其位（都 serial_match）。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"},
                            {"device": "/dev/sdb", "type": "sat"}])
    _fake_idents(monkeypatch, {"/dev/sda": _ident("SER-A"),
                               "/dev/sdb": _ident("SER-B")})
    for serial, dev in (("SER-A", "/dev/sda"), ("SER-B", "/dev/sdb")):
        cands, si = smart.resolve_smart_device(
            expected_serial=serial, expected_capacity_bytes=1000,
            expected_model="M")
        assert cands[0]["device"] == dev
        assert si["mapped_by"] == "serial_match"


def test_duplicate_serial_reports_ambiguity(monkeypatch) -> None:
    """-i 读到相同 serial → identity_ambiguity 非空。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"},
                            {"device": "/dev/sdb", "type": "sat"}])
    _fake_idents(monkeypatch, {"/dev/sda": _ident(EXP),
                               "/dev/sdb": _ident(EXP)})
    cands, si = smart.resolve_smart_device(expected_serial=EXP)
    assert si["identity_ambiguity"]
    assert "--serial" in si["identity_ambiguity"]
    assert si["mapped_by"] == "serial_match"


def test_no_serial_weak_matches_report_ambiguity(monkeypatch) -> None:
    """-i 无 serial、两台同型号同容量 → 歧义（弱匹配无法区分）。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"},
                            {"device": "/dev/sdb", "type": "sat"}])
    _fake_idents(monkeypatch, {"/dev/sda": _ident(None),
                               "/dev/sdb": _ident(None)})
    cands, si = smart.resolve_smart_device(
        expected_serial=EXP, expected_capacity_bytes=1000, expected_model="M")
    assert si["identity_ambiguity"]
    assert si["mapped_by"] == "capacity_match"  # 容量吻合档并列 → 不验证


def test_empty_scan_falls_back_with_risk(monkeypatch) -> None:
    """scan_devices() 空 → 候选含 fallback-sd 且标 unverified_index_mapping。"""
    _set_scan(monkeypatch, [])
    cands, si = smart.resolve_smart_device(
        expected_serial=EXP, disk_index=2)
    assert cands[0]["device"] == "/dev/sdc"
    assert cands[0]["risk"] == "unverified_index_mapping"
    assert si["mapped_by"] == "fallback"


def test_env_override_verified(monkeypatch) -> None:
    monkeypatch.setenv("CLDM_SMARTCTL_DEVICE", "/dev/sdz")
    cands, si = smart.resolve_smart_device(expected_serial=EXP)
    assert cands[0]["device"] == "/dev/sdz"
    assert si["mapped_by"] == "env_override"


def test_all_serials_mismatch_target_not_found(monkeypatch) -> None:
    """扫描表每一条都因序列号不符被剔除 → 空候选 + target_not_found，
    不追加任何未验证回退候选（已证明不是，而非无法证明）。
    （无盘符形态；有盘符时盘符直读优先、不判 target_not_found，
    见 test_letter_direct_target_not_found_suppressed。）"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"},
                            {"device": "/dev/sdb", "type": "sat"}])
    _fake_idents(monkeypatch, {"/dev/sda": _ident("SOMEONE-ELSE"),
                               "/dev/sdb": _ident("OTHER-DISK")})
    cands, si = smart.resolve_smart_device(
        expected_serial=EXP, disk_index=0)
    assert cands == []
    assert si["identity_risk"] == "target_not_found"
    assert si["identity_reason"]
    assert "目标盘" in si["identity_reason"]
    # candidates 里没有空 device 条目，也没有 sd/letter 回退
    assert all(c["device"] for c in si["candidates"])


def test_scan_empty_still_falls_back(monkeypatch) -> None:
    """回归：扫描表**为空**（无法证明）→ 仍走 sd 回退 + unverified_index_mapping。"""
    _set_scan(monkeypatch, [])
    cands, si = smart.resolve_smart_device(expected_serial=EXP, disk_index=2)
    assert cands[0]["device"] == "/dev/sdc"
    assert cands[0]["risk"] == "unverified_index_mapping"
    assert si.get("identity_risk") != "target_not_found"


def test_read_smart_verbose_empty_devices_no_fake_call(monkeypatch) -> None:
    """devices=[] → ok=False、attempts=[]，绝不伪造空设备候选/对 "" 发起调用。"""
    calls: list[list[str]] = []

    def fake_run(cmd):
        calls.append(cmd)
        return None

    monkeypatch.setattr(smart, "_run_cmd", fake_run)
    monkeypatch.setattr(smart, "_SCAN_CACHE", [])  # 扫描表缓存为空，不再发 --scan
    res = smart.read_smart_verbose(devices=[])
    assert res["ok"] is False
    assert res["attempts"] == []
    assert res["reason"] == "not_found"
    assert "没有可用的设备候选" in res["message"]
    assert res["device"] == ""
    assert calls == []  # 没有对空串发起任何 smartctl 调用
    # 旧单设备路径不受影响：给了 device 就照常尝试
    monkeypatch.setattr(smart, "_run_cmd_ex",
                        lambda cmd: (None, "not_found"))
    res2 = smart.read_smart_verbose("/dev/sdz")
    assert res2["ok"] is False and res2["attempts"]


def test_probe_linux_target_not_found_skips_read(monkeypatch, tmp_path) -> None:
    """Linux probe：目标盘不在扫描表 → 跳过读取、保留 probe 值、留原因 warning。"""
    import subprocess as sp

    from cold_manifest.probe import linux as pl
    from test_probe import LSBLK_JSON

    read_calls: list = []

    def fake_verbose(dev=None, *, devices=None, **kw):
        read_calls.append(devices)
        return {"ok": False, "raw": None, "device": "", "device_type": "",
                "exit_status": None, "reason": "not_found", "message": "x",
                "raw_excerpt": "", "attempts": [], "scan_info": {}}

    monkeypatch.setattr(smart, "resolve_smart_device", lambda **kw: (
        [], {"devices": [{"device": "/dev/sda", "type": "sat"}],
             "candidates": [], "device_used": None, "mapped_by": "fallback",
             "identity_risk": "target_not_found",
             "identity_reason": "扫描到的设备序列号均与目标盘不符"}))
    monkeypatch.setattr(smart, "read_smart_verbose", fake_verbose)
    monkeypatch.setattr(pl, "_run", lambda cmd, **kw: sp.CompletedProcess(
        cmd, 0, stdout=LSBLK_JSON, stderr=""))
    monkeypatch.setattr(pl, "find_mount_point",
                        lambda p: ("/mnt/cold", "/dev/sda1"))
    monkeypatch.setattr(pl.os, "statvfs", lambda mp: type(
        "V", (), {"f_bavail": 1, "f_frsize": 4096, "f_blocks": 100})())
    _volume, info = pl.probe_path_linux("/tmp", smartctl=True)
    assert read_calls == []  # 未发起任何 SMART 读取
    assert info.identity_verified is False
    assert info.smart_attempts is None
    assert info.identity_risk == "target_not_found"
    assert info.identity_reason
    assert info.disk_serial == "WD-WCC123"  # probe 值保留
    assert any("已跳过 SMART 读取" in w for w in (info.identity_warnings or []))


def test_read_device_identity_never_raises(monkeypatch) -> None:
    """-i 失败/无 JSON → None，绝不抛异常。"""

    def boom(cmd):
        raise RuntimeError("意外异常")

    monkeypatch.setattr(smart, "_run_cmd", boom)
    assert smart.read_device_identity("/dev/sda") is None


# ---------------------------------------------------------------- probe 条件覆盖

_PS_JSON = json.dumps({
    "volume": {"fs": "ntfs", "label": "X", "vserial": "1A2B3C4D",
               "size": 1000, "free": 1},
    "partition": {"guid": "G", "index": 1, "offset": 0, "size": 1000,
                  "mbrType": 0, "gptType": 0},
    "disk": {"index": 0, "model": "M", "serial": "A", "interface": "USB",
             "firmware": "F", "size": 1000, "friendlyName": "FN",
             "busType": "USB", "partitionStyle": "GPT"},
})


def _run_probe(monkeypatch, *, mapped_by: str, smart_serial: "str | None",
               ambiguity: "str | None" = None) -> "object":
    """跑 probe_path_win 的 smartctl 段：probe serial=A，smartctl 读到 smart_serial。"""

    class _FakePath(str):
        def exists(self):
            return True

        def resolve(self):
            return self

    monkeypatch.setattr(pw, "Path", _FakePath)
    monkeypatch.setattr(pw, "run_powershell", lambda script, timeout=60: _PS_JSON)
    si = {"mapped_by": mapped_by, "devices": [], "candidates": [],
          "device_used": None, "mapped_from_scan": True}
    if ambiguity:
        si["identity_ambiguity"] = ambiguity
    monkeypatch.setattr(smart, "resolve_smart_device", lambda **kw:
                        ([{"device": "/dev/sda", "type": "sat",
                           "source": "scan"}], si))
    raw = json.dumps({"serial_number": smart_serial or "",
                      "model_name": "M",
                      "user_capacity": {"bytes": 1000}})
    monkeypatch.setattr(smart, "read_smart_verbose", lambda dev=None, *, devices=None, **kw: {
        "ok": True, "raw": raw, "device": "/dev/sda", "device_type": "sat",
        "exit_status": 0, "reason": None, "message": None,
        "raw_excerpt": raw, "attempts": [],
        "device_candidates": ["/dev/sda"], "scan_info": {}})
    volume, info = pw.probe_path_win("E:\\", smartctl=True)
    return info


def test_probe_unverified_keeps_probe_serial(monkeypatch) -> None:
    """⑥ 未验证：probe serial=A、smartctl serial=B → 保留 A，B 进 physical_serial。"""
    info = _run_probe(monkeypatch, mapped_by="weak_match", smart_serial="B")
    assert info.disk_serial == "A"
    assert info.serial_source == "probe"
    assert info.physical_serial == "B"
    assert info.identity_verified is False
    assert any("为防错盘已保留探测值" in w for w in (info.identity_warnings or []))


def test_probe_verified_overrides_serial(monkeypatch) -> None:
    """⑦ 已验证（serial_match）：覆盖为 B、serial_source=smartctl。"""
    info = _run_probe(monkeypatch, mapped_by="serial_match", smart_serial="B")
    assert info.disk_serial == "B"
    assert info.serial_source == "smartctl"
    assert info.identity_verified is True
    # 覆盖时与 probe 值不同 → warning 留痕
    assert any("不一致" in w for w in (info.identity_warnings or []))


def test_probe_ambiguity_blocks_override(monkeypatch) -> None:
    """③ probe 侧：serial_match 但有歧义 → 不覆盖、identity_verified=false。"""
    info = _run_probe(monkeypatch, mapped_by="serial_match", smart_serial="B",
                      ambiguity="多台设备均报告相同序列号")
    assert info.disk_serial == "A"
    assert info.identity_verified is False
    assert info.identity_ambiguity


def test_probe_unverified_fills_empty_probe_serial(monkeypatch) -> None:
    """probe 序列号为空且未验证 → 允许补全，标 smartctl_unverified。"""
    class _FakePath(str):
        def exists(self):
            return True

        def resolve(self):
            return self

    monkeypatch.setattr(pw, "Path", _FakePath)
    ps = json.loads(_PS_JSON)
    ps["disk"]["serial"] = ""
    monkeypatch.setattr(pw, "run_powershell",
                        lambda script, timeout=60: json.dumps(ps))
    si = {"mapped_by": "fallback", "candidates": [], "devices": [],
          "device_used": None, "mapped_from_scan": False}
    monkeypatch.setattr(smart, "resolve_smart_device", lambda **kw:
                        ([{"device": "/dev/sda", "type": "sat",
                           "source": "fallback-sd",
                           "risk": "unverified_index_mapping"}], si))
    raw = json.dumps({"serial_number": "B", "model_name": "M"})
    monkeypatch.setattr(smart, "read_smart_verbose", lambda dev=None, *, devices=None, **kw: {
        "ok": True, "raw": raw, "device": "/dev/sda", "device_type": "sat",
        "exit_status": 0, "reason": None, "message": None,
        "raw_excerpt": raw, "attempts": [],
        "device_candidates": ["/dev/sda"], "scan_info": {}})
    _volume, info = pw.probe_path_win("E:\\", smartctl=True)
    assert info.disk_serial == "B"
    assert info.serial_source == "smartctl_unverified"
    assert info.identity_verified is False


# ---------------------------------------------------------------- 盒 ID 场景（真机缺陷修复）

BOX_ID = "20260123004775F"   # USB 盒上报的 ID（探针序列号）
REAL_4T = "16NDT0O1T"        # 4T 真盘序列号
REAL_2T = "2T-TRUE-SER"      # 2T 真盘序列号
CAP_4T = 4000787030016
CAP_2T = 2000398934016


def test_box_id_capacity_unique_match_verified(monkeypatch) -> None:
    """用户场景：expected=盒 ID，scan=[4T 真盘、2T 真盘] → 2T 容量不符被
    剔除，4T 保留并判已验证（unique_capacity_match），identity_reason 提及
    盒 ID 与真盘序列号。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"},
                            {"device": "/dev/sdb", "type": "sat"}])
    _fake_idents(monkeypatch, {
        "/dev/sda": _ident(REAL_4T, model="TOSHIBA MG09", cap=CAP_4T),
        "/dev/sdb": _ident(REAL_2T, model="OTHER", cap=CAP_2T)})
    cands, si = smart.resolve_smart_device(
        expected_serial=BOX_ID, expected_capacity_bytes=CAP_4T,
        expected_model="TOSHIBA EXTERNAL_USB")
    assert [c["device"] for c in cands if c["source"] == "scan"] == ["/dev/sda"]
    assert si["mapped_by"] == "unique_capacity_match"
    assert si.get("identity_ambiguity") is None
    reason = si.get("identity_reason") or ""
    assert BOX_ID in reason and REAL_4T in reason
    assert "3.6TB" in reason or "4.0TB" in reason


def test_p300_two_capacity_hits_not_verified(monkeypatch) -> None:
    """P300 场景：两台候选容量/型号全同（各读到真序列号）→ 不得验证，
    identity_ambiguity 非空。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"},
                            {"device": "/dev/sdb", "type": "sat"}])
    _fake_idents(monkeypatch, {
        "/dev/sda": _ident("P300-A", model="P300", cap=1000),
        "/dev/sdb": _ident("P300-B", model="P300", cap=1000)})
    cands, si = smart.resolve_smart_device(
        expected_serial=BOX_ID, expected_capacity_bytes=1000,
        expected_model="P300")
    assert si["mapped_by"] == "capacity_match"
    assert si["identity_ambiguity"]
    assert "无法确定" in si["identity_ambiguity"]


def test_scan_empty_unverified_reason(monkeypatch) -> None:
    """scan 为空 → 未验证 + 理由文案。"""
    _set_scan(monkeypatch, [])
    _cands, si = smart.resolve_smart_device(
        expected_serial=BOX_ID, expected_capacity_bytes=CAP_4T)
    assert si["mapped_by"] == "fallback"
    assert "扫描表为空" in (si.get("identity_reason") or "")


def test_no_serial_read_unverified_reason(monkeypatch) -> None:
    """唯一候选但读不到序列号（盒型号匹配 → 80）：单台唯一高分 → 判已验证
    （unique_capacity_match），identity_reason 说明"未读到序列号、按容量+型号
    唯一定位"。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"}])
    _fake_idents(monkeypatch, {"/dev/sda": _ident(None, model="M", cap=1000)})
    _cands, si = smart.resolve_smart_device(
        expected_serial=BOX_ID, expected_capacity_bytes=1000,
        expected_model="M")
    assert si["mapped_by"] == "unique_capacity_match"
    reason = si.get("identity_reason") or ""
    assert BOX_ID in reason and "唯一定位" in reason


def test_no_serial_weak_multi_not_verified_reason(monkeypatch) -> None:
    """读不到序列号且多台并列 → 不验证 + 歧义/理由文案。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"},
                            {"device": "/dev/sdb", "type": "sat"}])
    _fake_idents(monkeypatch, {"/dev/sda": _ident(None, model="M", cap=1000),
                               "/dev/sdb": _ident(None, model="M", cap=1000)})
    _cands, si = smart.resolve_smart_device(
        expected_serial=BOX_ID, expected_capacity_bytes=1000,
        expected_model="M")
    assert si["mapped_by"] == "capacity_match"
    assert si["identity_ambiguity"]


def test_same_capacity_diff_model_model_wins_verified(monkeypatch) -> None:
    """JMicron 2T USB（读不到序列号，盒型号匹配 → 80）vs 内置 WD SN570
    （真盘序列号不同 + 同容量但型号不符 → 70）：top 唯一且领先一档 →
    靠型号唯一胜出判已验证。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"},
                            {"device": "/dev/sdb", "type": "sat"}])
    _fake_idents(monkeypatch, {
        "/dev/sda": _ident(None, model="JMicron Generic USB", cap=2000398934016),
        "/dev/sdb": _ident("WD-SN570-SER", model="WD Blue SN570",
                           cap=2000398934016)})
    cands, si = smart.resolve_smart_device(
        expected_serial="0123456789ABCDEF", expected_capacity_bytes=2000398934016,
        expected_model="JMicron Generic USB")
    assert cands[0]["device"] == "/dev/sda"
    assert si["mapped_by"] == "unique_capacity_match"
    assert si.get("identity_ambiguity") is None
    assert "唯一定位" in (si.get("identity_reason") or "")


def test_single_usb_disk_verified(monkeypatch) -> None:
    """只插一块 USB 盘：候选唯一即 top 唯一（无第二名）→ 验证通过。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"}])
    _fake_idents(monkeypatch, {
        "/dev/sda": _ident(REAL_4T, model="TOSHIBA MG09", cap=CAP_4T)})
    cands, si = smart.resolve_smart_device(
        expected_serial=BOX_ID, expected_capacity_bytes=CAP_4T,
        expected_model="TOSHIBA EXTERNAL_USB")
    assert si["mapped_by"] == "unique_capacity_match"
    assert REAL_4T in (si.get("identity_reason") or "")


# ---------------------------------------------------------------- 总线硬区分 + 并列不猜读（P0：SN570 错认场景）

SN570_SER = "23024Q800919"   # 内置 WD SN570 的真序列号（用户误读到的那个）
P300_SER = "X0DG6A2GS"       # P300 真盘序列号
BOX_P300 = "JMBOX-000111"    # P300 盒上报 ID
CAP_2T = 2000398934016


def test_usb_target_never_picks_nvme_candidate(monkeypatch) -> None:
    """复刻用户场景：目标 USB 盒（2TB、JMicron 盒型号），扫描表里内置
    NVMe（SN570，同容量 type=nvme）排在 USB 盘前面 → NVMe 必须被总线
    区分剔除，选中 USB 盘里的 P300，绝不读到 SN570 的序列号。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "nvme"},
                            {"device": "/dev/sdc", "type": "sat"}])
    _fake_idents(monkeypatch, {
        "/dev/sda": _ident(SN570_SER, model="WD Blue SN570", cap=CAP_2T),
        "/dev/sdc": _ident(P300_SER, model="TOSHIBA MG09ACA20TE", cap=CAP_2T)})
    cands, si = smart.resolve_smart_device(
        expected_serial=BOX_P300, expected_capacity_bytes=CAP_2T,
        expected_model="JMicron Generic SCSI Disk Device",
        expected_interface="USB")
    assert [c["device"] for c in cands] == ["/dev/sdc"]
    assert si["mapped_by"] == "unique_capacity_match"
    assert si.get("identity_ambiguity") is None
    assert all(c["device"] != "/dev/sda" for c in si["candidates"])
    assert (si["candidates"][0].get("identity_serial")) == P300_SER


def test_nvme_target_never_picks_usb_candidate(monkeypatch) -> None:
    """对称场景：目标内置 NVMe → sat/USB 候选被剔除，只留 nvme。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "nvme"},
                            {"device": "/dev/sdc", "type": "sat"}])
    _fake_idents(monkeypatch, {
        "/dev/sda": _ident(SN570_SER, model="WD Blue SN570", cap=CAP_2T),
        "/dev/sdc": _ident(P300_SER, model="TOSHIBA MG09ACA20TE", cap=CAP_2T)})
    cands, si = smart.resolve_smart_device(
        expected_serial=SN570_SER, expected_capacity_bytes=CAP_2T,
        expected_model="WD Blue SN570", expected_interface="NVMe")
    assert [c["device"] for c in cands] == ["/dev/sda"]
    assert si["mapped_by"] == "serial_match"


def test_true_tie_same_bus_no_blind_read(monkeypatch) -> None:
    """真并列：两台 sat 同容量同型号、都读不到序列号 → 不验证，且候选链
    整链收回（含 sd/letter 回退）——不给调用方"按顺序读第一个"的机会。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"},
                            {"device": "/dev/sdb", "type": "sat"}])
    _fake_idents(monkeypatch, {"/dev/sda": _ident(None, model="P300", cap=CAP_2T),
                               "/dev/sdb": _ident(None, model="P300", cap=CAP_2T)})
    cands, si = smart.resolve_smart_device(
        expected_serial=BOX_P300, expected_capacity_bytes=CAP_2T,
        expected_model="JMicron Generic SCSI Disk Device",
        expected_interface="USB", disk_index=0)
    assert cands == []                       # 无候选可读（宁缺勿错）
    assert si["mapped_by"] == "capacity_match"  # 口径保留：并列不验证
    assert si["identity_ambiguity"]
    assert si["identity_risk"] == "identity_ambiguity"
    assert all(c["device"] not in ("/dev/sda", "/dev/sdb", "E:")
               for c in si["candidates"])


def test_e2e_probe_p300_vs_sn570_order_independent(monkeypatch, capsys) -> None:
    """端到端（打桩 scan + 打桩 -i）：内置 NVMe 与 USB 盒两种候选顺序下，
    probe 都必须选中 USB 盘（读到 P300 真序列号），与候选顺序无关。"""

    class _FakePath(str):
        def exists(self):
            return True

        def resolve(self):
            return self

    ps = json.loads(_PS_JSON)
    ps["disk"]["serial"] = BOX_P300
    ps["disk"]["model"] = "JMicron Generic SCSI Disk Device"
    ps["disk"]["busType"] = "USB"
    ps["disk"]["size"] = CAP_2T
    ps["volume"]["size"] = CAP_2T - 1000

    scan_entries = [{"device": "/dev/sda", "type": "nvme"},
                    {"device": "/dev/sdc", "type": "sat"}]
    idents = {
        "/dev/sda": _ident(SN570_SER, model="WD Blue SN570", cap=CAP_2T),
        "/dev/sdc": _ident(P300_SER, model="TOSHIBA MG09ACA20TE", cap=CAP_2T)}
    raw_p300 = json.dumps({"serial_number": P300_SER,
                           "model_name": "TOSHIBA MG09ACA20TE",
                           "user_capacity": {"bytes": CAP_2T}})

    read_devices: list[str] = []

    for order in (scan_entries, list(reversed(scan_entries))):
        monkeypatch.setattr(pw, "Path", _FakePath)
        monkeypatch.setattr(pw, "run_powershell",
                            lambda script, timeout=60: json.dumps(ps))
        _set_scan(monkeypatch, order)
        _fake_idents(monkeypatch, idents)

        def fake_verbose(dev=None, *, devices=None, **kw):
            got = devices[0]["device"]
            read_devices.append(got)
            return {"ok": True, "raw": raw_p300, "device": got,
                    "device_type": "sat", "exit_status": 0, "reason": None,
                    "message": None, "raw_excerpt": raw_p300, "attempts": [],
                    "device_candidates": [got], "scan_info": {}}

        monkeypatch.setattr(smart, "read_smart_verbose", fake_verbose)
        _volume, info = pw.probe_path_win("E:\\", smartctl=True)
        rsi = info.smart_scan_info or {}
        print(f"order={[e['device'] for e in order]} "
              f"mapped_by={rsi.get('mapped_by')} "
              f"read_device={read_devices[-1]} "
              f"serial={info.disk_serial}")
        assert rsi["mapped_by"] == "letter_direct"     # 盘符直读优先
        assert read_devices[-1] == "E:"                # 读盘符，不碰 /dev/sd*
        assert all(not d.startswith("/dev/") for d in read_devices)
        assert info.identity_verified is True
        assert info.disk_serial == P300_SER                   # P300 真序列号
        assert info.physical_serial == P300_SER
        assert info.serial_source == "smartctl"
    # 两种候选顺序读的是同一台设备、同一序列号
    assert read_devices == ["E:", "E:"]


# ---------------------------------------------------------------- 读后校验（最后一道硬保险）

_NVME_RAW = json.dumps({
    "device": {"name": "/dev/sda", "type": "nvme", "protocol": "NVMe"},
    "model_name": "WD Blue SN570",
    "serial_number": SN570_SER,
    "user_capacity": {"bytes": CAP_2T},
    "smart_status": {"passed": True},
})
_SAT_RAW = json.dumps({
    "device": {"name": "/dev/sdc", "type": "sat", "protocol": "ATA"},
    "model_name": "TOSHIBA MG09ACA20TE",
    "serial_number": P300_SER,
    "user_capacity": {"bytes": CAP_2T},
    "smart_status": {"passed": True},
})


def _fake_raw_by_dev(monkeypatch, raw_by_dev: dict) -> None:
    def fake_run_ex(cmd):
        dev = cmd[-1]
        raw = raw_by_dev.get(dev)
        if raw is None:
            return None, "not_found"
        import subprocess as sp
        return sp.CompletedProcess(cmd, 0, stdout=raw, stderr=""), None
    monkeypatch.setattr(smart, "_run_cmd_ex", fake_run_ex)


def test_read_verify_drops_nvme_read_for_usb_target(monkeypatch) -> None:
    """读后校验：USB 桥目标（候选链被猜着读时最坏情况只有 NVMe 一条）——
    读到 nvme 设备必须整包丢弃（ok=False + 中文原因），绝不返回给调用方。"""
    _fake_raw_by_dev(monkeypatch, {"/dev/sda": _NVME_RAW})
    monkeypatch.setattr(smart, "_SCAN_CACHE", [])
    res = smart.read_smart_verbose(
        devices=[{"device": "/dev/sda", "type": "sat", "source": "scan"}],
        expect_interface="USB", expect_capacity_bytes=CAP_2T,
        expect_model="JMicron Generic SCSI Disk Device")
    assert res["ok"] is False                       # 绝不把 NVMe 读取当成功
    assert res["reason"] == "identity_mismatch"
    assert res["raw"] is None                       # 原始数据整包丢弃
    assert "不相容" in res["message"]
    assert res["attempts"][0]["error"] == "identity_mismatch"
    assert "总线不相容" in res["attempts"][0]["stdout_messages"]


def test_read_verify_falls_through_to_next_candidate(monkeypatch) -> None:
    """候选链 [nvme, sat]：NVMe 读取被校验丢弃后继续尝试下一台，最终
    采纳 sat 那台（USB 盘里的 P300）。"""
    _fake_raw_by_dev(monkeypatch, {"/dev/sda": _NVME_RAW,
                                   "/dev/sdc": _SAT_RAW})
    monkeypatch.setattr(smart, "_SCAN_CACHE", [])
    res = smart.read_smart_verbose(
        devices=[{"device": "/dev/sda", "type": "sat", "source": "scan"},
                 {"device": "/dev/sdc", "type": "sat", "source": "scan"}],
        expect_interface="USB", expect_capacity_bytes=CAP_2T)
    assert res["ok"] is True
    assert res["device"] == "/dev/sdc"
    assert P300_SER in res["raw"]


def test_read_verify_capacity_mismatch_dropped(monkeypatch) -> None:
    """同总线但容量差 >20% → 丢弃。"""
    big = json.loads(_SAT_RAW)
    big["user_capacity"] = {"bytes": 4000787030016}   # 4T vs 目标 2T
    _fake_raw_by_dev(monkeypatch, {"/dev/sdc": json.dumps(big)})
    monkeypatch.setattr(smart, "_SCAN_CACHE", [])
    res = smart.read_smart_verbose(
        devices=[{"device": "/dev/sdc", "type": "sat", "source": "scan"}],
        expect_interface="USB", expect_capacity_bytes=CAP_2T)
    assert res["ok"] is False
    assert res["reason"] == "identity_mismatch"
    assert "容量不相符" in res["attempts"][0]["stdout_messages"]


def test_read_verify_compatible_read_passes(monkeypatch) -> None:
    """相容读取（sat + 容量吻合）不受影响。"""
    _fake_raw_by_dev(monkeypatch, {"/dev/sdc": _SAT_RAW})
    monkeypatch.setattr(smart, "_SCAN_CACHE", [])
    res = smart.read_smart_verbose(
        devices=[{"device": "/dev/sdc", "type": "sat", "source": "scan"}],
        expect_interface="USB", expect_capacity_bytes=CAP_2T)
    assert res["ok"] is True
    assert P300_SER in res["raw"]


def test_probe_discards_mismatched_read(monkeypatch) -> None:
    """probe 级：resolve 极端劣化后只给了 NVMe 候选（真实 read_smart_verbose
    走桩 _run_cmd 读到 SN570）→ 读取必须被丢弃：smart_status=unavailable、
    probe 盒 ID 序列号原样保留、SN570 序列号绝不落进结果与 warnings 之外
    的任何字段。"""

    class _FakePath(str):
        def exists(self):
            return True

        def resolve(self):
            return self

    ps = json.loads(_PS_JSON)
    ps["disk"]["serial"] = BOX_P300
    ps["disk"]["model"] = "JMicron Generic SCSI Disk Device"
    ps["disk"]["size"] = CAP_2T
    ps["volume"]["size"] = CAP_2T - 1000
    monkeypatch.setattr(pw, "Path", _FakePath)
    monkeypatch.setattr(pw, "run_powershell",
                        lambda script, timeout=60: json.dumps(ps))
    si = {"mapped_by": "fallback", "devices": [], "candidates": [],
          "device_used": None, "mapped_from_scan": False}
    monkeypatch.setattr(smart, "resolve_smart_device", lambda **kw:
                        ([{"device": "/dev/sda", "type": "sat",
                           "source": "fallback-sd",
                           "risk": "unverified_index_mapping"}], si))
    _fake_raw_by_dev(monkeypatch, {"/dev/sda": _NVME_RAW})
    monkeypatch.setattr(smart, "_SCAN_CACHE", [])
    _volume, info = pw.probe_path_win("E:\\", smartctl=True)
    assert info.smart_status == "unavailable"        # 读取被丢弃
    assert info.smart_raw is None                    # NVMe 原始数据绝不入库
    assert info.disk_serial == BOX_P300              # probe 盒 ID 原样保留
    assert info.physical_serial != SN570_SER
    assert info.physical_serial in ("", None)
    assert info.smart_error and "不相容" in info.smart_error
    assert info.identity_verified is False


# ---------------------------------------------------------------- 真机回归夹具（cldm-diag.txt，2026-10-03 实测）
# 用户机器真实设备表（smartctl 7.5 / Win11）：
#   scan-open: sda nvme(三星1TB) / sdb nvme(WD SN570 2TB, 23024Q800919)
#              / sdc sat(P300 TOSHIBA HDWD120, X0DG6A2GS) / sdd sat(4T MQ04UBB400)
#   采 2TB P300（JMicron 盒）：Win32_DiskDrive Index=2 serial=0123456789ABCDEF
#   model="JMicron Generic SCSI Disk Device" size=2000396321280, BusType=USB。
#   缺陷：sdb 与 sdc 容量完全相同（2000398934016）且都与盒型号不符 →
#   同分 70 并列 → 按扫描顺序读了 sdb，把 SN570 序列号记到 P300 名下。

_DIAG_SCAN = [{"device": "/dev/sda", "type": "nvme"},
              {"device": "/dev/sdb", "type": "nvme"},
              {"device": "/dev/sdc", "type": "sat"},
              {"device": "/dev/sdd", "type": "sat"}]

_SDA_RAW = json.dumps({
    "device": {"name": "/dev/sda", "type": "nvme", "protocol": "NVMe"},
    "model_name": "SAMSUNG MZVL81T0HELB-00BTW",
    "serial_number": "S7HZNX0Y447160",
    "user_capacity": {"bytes": 1024209543168}})

_SDB_RAW = json.dumps({
    "device": {"name": "/dev/sdb", "type": "nvme", "protocol": "NVMe"},
    "model_name": "WD Blue SN570 2TB SSD",
    "serial_number": "23024Q800919",
    "user_capacity": {"bytes": 2000398934016}})

_SDC_RAW = json.dumps({
    "device": {"name": "/dev/sdc", "type": "sat", "protocol": "ATA"},
    "model_family": "Toshiba P300 (CMR)",
    "model_name": "TOSHIBA HDWD120",
    "serial_number": "X0DG6A2GS",
    "user_capacity": {"bytes": 2000398934016}})

_SDD_RAW = json.dumps({
    "device": {"name": "/dev/sdd", "type": "sat", "protocol": "ATA"},
    "model_family": "Toshiba 2.5\" HDD MQ04UBB... (USB, SMR)",
    "model_name": "TOSHIBA MQ04UBB400",
    "serial_number": "16NDT0O1T",
    "user_capacity": {"bytes": 4000787030016}})

_DIAG_IDENTS = {
    "/dev/sda": {"serial": "S7HZNX0Y447160", "capacity_bytes": 1024209543168,
                 "model": "SAMSUNG MZVL81T0HELB-00BTW",
                 "device_type": "nvme", "raw": _SDA_RAW},
    "/dev/sdb": {"serial": "23024Q800919", "capacity_bytes": 2000398934016,
                 "model": "WD Blue SN570 2TB SSD",
                 "device_type": "nvme", "raw": _SDB_RAW},
    "/dev/sdc": {"serial": P300_SER, "capacity_bytes": CAP_2T,
                 "model": "TOSHIBA HDWD120",
                 "device_type": "sat", "raw": _SDC_RAW},
    "/dev/sdd": {"serial": "16NDT0O1T", "capacity_bytes": 4000787030016,
                 "model": "TOSHIBA MQ04UBB400",
                 "device_type": "sat", "raw": _SDD_RAW},
}
JM_BOX_SER = "0123456789ABCDEF"
JM_BOX_MODEL = "JMicron Generic SCSI Disk Device"
JM_WIN32_SIZE = 2000396321280   # Win32_DiskDrive 上报的（略小的）容量


def test_real_diag_bus_filter_selects_sdc(monkeypatch) -> None:
    """真机回归：采 2TB P300（JMicron 盒）→ 总线过滤剔除两台 nvme、
    4T 被容量剔除 → 唯一选中 sdc（X0DG6A2GS），绝不并列、绝不读 sdb。"""
    _set_scan(monkeypatch, _DIAG_SCAN)
    _fake_idents(monkeypatch, _DIAG_IDENTS)
    cands, si = smart.resolve_smart_device(
        expected_serial=JM_BOX_SER, expected_capacity_bytes=JM_WIN32_SIZE,
        expected_model=JM_BOX_MODEL, expected_interface="USB")
    assert [c["device"] for c in cands] == ["/dev/sdc"]
    assert si["mapped_by"] == "unique_capacity_match"
    assert si.get("identity_ambiguity") is None
    assert cands[0].get("identity_serial") == P300_SER
    assert all("23024Q800919" != c.get("identity_serial")
               for c in si["candidates"])


def test_real_diag_no_interface_still_no_blind_read(monkeypatch) -> None:
    """真机对照：即使没有接口信息（expected_interface=None），sdb/sdc
    容量同分并列 → 并列不猜读：候选链收回，SN570 序列号读不到。"""
    _set_scan(monkeypatch, _DIAG_SCAN)
    _fake_idents(monkeypatch, _DIAG_IDENTS)
    cands, si = smart.resolve_smart_device(
        expected_serial=JM_BOX_SER, expected_capacity_bytes=JM_WIN32_SIZE,
        expected_model=JM_BOX_MODEL, disk_index=2)
    assert si["identity_ambiguity"]
    assert si["mapped_by"] == "capacity_match"     # 并列不验证
    assert "/dev/sdb" not in [c["device"] for c in cands]
    assert "/dev/sdc" not in [c["device"] for c in cands]
    assert cands == []                             # 无候选可"猜着读"


def test_real_diag_read_verify_layer(monkeypatch) -> None:
    """真机读后校验：就算劣化到把 sdb 当候选去读，SN570 的读取也被整包
    丢弃，最终采纳的是 sdc 的 P300 序列号。"""
    _fake_raw_by_dev(monkeypatch, {"/dev/sdb": _SDB_RAW, "/dev/sdc": _SDC_RAW})
    monkeypatch.setattr(smart, "_SCAN_CACHE", [])
    res = smart.read_smart_verbose(
        devices=[{"device": "/dev/sdb", "type": "nvme", "source": "scan"},
                 {"device": "/dev/sdc", "type": "sat", "source": "scan"}],
        expect_interface="USB", expect_capacity_bytes=JM_WIN32_SIZE,
        expect_model=JM_BOX_MODEL)
    assert res["ok"] is True
    assert res["device"] == "/dev/sdc"
    assert P300_SER in res["raw"]
    assert "23024Q800919" not in res["raw"]
    assert any(a.get("error") == "identity_mismatch"
               for a in res["attempts"])


def test_real_diag_e2e_probe_p300(monkeypatch) -> None:
    """真机端到端：probe（Win32 Index=2 / JMicron 盒 / USB / 2TB）→
    全链（resolve + 真实 read_smart_verbose 读桩数据）结果必须是 P300：
    identity_verified=True、disk_serial=X0DG6A2GS；SN570 的
    23024Q800919 绝不出现在 disk_serial/physical_serial。"""

    class _FakePath(str):
        def exists(self):
            return True

        def resolve(self):
            return self

    ps = json.loads(_PS_JSON)
    ps["disk"].update({"index": 2, "serial": JM_BOX_SER, "model": JM_BOX_MODEL,
                       "busType": "USB", "size": JM_WIN32_SIZE})
    ps["volume"]["size"] = JM_WIN32_SIZE - 1000
    monkeypatch.setattr(pw, "Path", _FakePath)
    monkeypatch.setattr(pw, "run_powershell",
                        lambda script, timeout=60: json.dumps(ps))
    _set_scan(monkeypatch, _DIAG_SCAN)
    _fake_idents(monkeypatch, _DIAG_IDENTS)
    _fake_raw_by_dev(monkeypatch, {"/dev/sda": _SDA_RAW, "/dev/sdb": _SDB_RAW,
                                   "/dev/sdc": _SDC_RAW, "/dev/sdd": _SDD_RAW})
    _volume, info = pw.probe_path_win("E:\\", smartctl=True)
    rsi = info.smart_scan_info or {}
    # 有盘符 → letter_direct 策略；桩数据里 E: 打不开 → 兜底路径按总线
    # 过滤选中 sdc，结果仍是 P300，SN570 序列号绝不出现
    assert rsi["mapped_by"] == "letter_direct"
    assert rsi["device_used"] == "/dev/sdc"
    assert info.identity_verified is True
    assert info.disk_serial == P300_SER
    assert info.physical_serial == P300_SER
    assert info.serial_source == "smartctl"
    assert info.disk_serial != "23024Q800919"
    assert info.physical_serial != "23024Q800919"


# ---------------------------------------------------------------- 盘符直读优先（设计定案）

def test_letter_direct_preferred_when_available(monkeypatch) -> None:
    """有盘符：盘符候选排最前（letter_direct），扫描表候选退居兜底；
    不再依赖容量猜（并列也不影响盘符直读）。"""
    _set_scan(monkeypatch, _DIAG_SCAN)
    _fake_idents(monkeypatch, _DIAG_IDENTS)
    cands, si = smart.resolve_smart_device(
        expected_serial=JM_BOX_SER, expected_capacity_bytes=JM_WIN32_SIZE,
        expected_model=JM_BOX_MODEL, expected_interface="USB",
        disk_index=2, letter="E")
    assert cands[0]["device"] == "E:"
    assert cands[0]["source"] == "letter"
    assert si["mapped_by"] == "letter_direct"
    # 兜底：扫描表里总线相容的 sdc 仍保留在链尾
    assert [c["device"] for c in cands[1:] if c["source"] == "scan"] \
        == ["/dev/sdc"]
    assert "盘符" in (si.get("identity_reason") or "")


def test_letter_direct_beats_capacity_tie(monkeypatch) -> None:
    """同容量极多盘（2×990EVOPlus + 多根铠侠 2TB 场景）：容量完全无法区分，
    有盘符照样 letter_direct 唯一确定。"""
    scan = [{"device": "/dev/sda", "type": "nvme"},
            {"device": "/dev/sdb", "type": "nvme"},
            {"device": "/dev/sdc", "type": "nvme"}]
    idents = {d["device"]: _ident(f"S-{d['device'][-1]}",
                                  model="SSD-2TB", cap=CAP_2T)
              for d in scan}
    _set_scan(monkeypatch, scan)
    _fake_idents(monkeypatch, idents)
    cands, si = smart.resolve_smart_device(
        expected_serial="0123456789ABCDEF", expected_capacity_bytes=CAP_2T,
        expected_model="Generic", expected_interface="USB", letter="F")
    assert cands[0]["device"] == "F:"
    assert si["mapped_by"] == "letter_direct"


def test_letter_read_used_and_scan_fallback_not_read(monkeypatch) -> None:
    """probe 端到端：盘符读取成功 → 读的是 E:，扫描表候选一台都没碰；
    序列号采信盘符读取结果（P300 真序列号），23024Q800919 绝不出现。"""

    class _FakePath(str):
        def exists(self):
            return True

        def resolve(self):
            return self

    ps = json.loads(_PS_JSON)
    ps["disk"].update({"index": 2, "serial": JM_BOX_SER, "model": JM_BOX_MODEL,
                       "busType": "USB", "size": JM_WIN32_SIZE})
    ps["volume"]["size"] = JM_WIN32_SIZE - 1000
    monkeypatch.setattr(pw, "Path", _FakePath)
    monkeypatch.setattr(pw, "run_powershell",
                        lambda script, timeout=60: json.dumps(ps))
    _set_scan(monkeypatch, _DIAG_SCAN)
    _fake_idents(monkeypatch, _DIAG_IDENTS)
    read_devs: list[str] = []

    def fake_run_ex(cmd):
        import subprocess as sp
        dev = cmd[-1]
        read_devs.append(dev)
        raw = {"E:": _SDC_RAW, "/dev/sdc": _SDC_RAW}.get(dev)
        if raw is None:
            return None, "not_found"
        return sp.CompletedProcess(cmd, 0, stdout=raw, stderr=""), None

    monkeypatch.setattr(smart, "_run_cmd_ex", fake_run_ex)
    _volume, info = pw.probe_path_win("E:\\", smartctl=True)
    rsi = info.smart_scan_info or {}
    assert rsi["mapped_by"] == "letter_direct"
    assert rsi["device_used"] == "E:"
    assert read_devs[0] == "E:"                    # 盘符直读
    assert "/dev/sdb" not in read_devs             # 扫描候选一台没读
    assert "/dev/sda" not in read_devs
    assert info.identity_verified is True
    assert info.disk_serial == P300_SER
    assert info.disk_serial != "23024Q800919"
    assert info.physical_serial != "23024Q800919"


def test_letter_read_failure_falls_back_to_scan(monkeypatch) -> None:
    """盘符打不开 → 回退扫描表兜底路径：总线过滤选中 sdc，读到 P300；
    SN570（sdb）被总线剔除 + 读后校验双保险。"""

    class _FakePath(str):
        def exists(self):
            return True

        def resolve(self):
            return self

    ps = json.loads(_PS_JSON)
    ps["disk"].update({"index": 2, "serial": JM_BOX_SER, "model": JM_BOX_MODEL,
                       "busType": "USB", "size": JM_WIN32_SIZE})
    ps["volume"]["size"] = JM_WIN32_SIZE - 1000
    monkeypatch.setattr(pw, "Path", _FakePath)
    monkeypatch.setattr(pw, "run_powershell",
                        lambda script, timeout=60: json.dumps(ps))
    _set_scan(monkeypatch, _DIAG_SCAN)
    _fake_idents(monkeypatch, _DIAG_IDENTS)
    read_devs: list[str] = []

    def fake_run_ex(cmd):
        import subprocess as sp
        dev = cmd[-1]
        read_devs.append(dev)
        raw = {"/dev/sdc": _SDC_RAW}.get(dev)
        if raw is None:
            return None, "not_found"   # E: 打不开，其余设备逐台尝试
        return sp.CompletedProcess(cmd, 0, stdout=raw, stderr=""), None

    monkeypatch.setattr(smart, "_run_cmd_ex", fake_run_ex)
    _volume, info = pw.probe_path_win("E:\\", smartctl=True)
    rsi = info.smart_scan_info or {}
    assert read_devs[0] == "E:"                    # 先试盘符
    assert rsi["device_used"] == "/dev/sdc"        # 失败 → 兜底选中 sdc
    assert info.identity_verified is True
    assert info.disk_serial == P300_SER
    assert "/dev/sdb" not in read_devs             # nvme 被总线过滤剔除
    assert info.disk_serial != "23024Q800919"


def test_letter_direct_target_not_found_suppressed(monkeypatch) -> None:
    """有盘符时不判 target_not_found（盘符直读不依赖扫描表）：扫描表全部
    序列号不符也不阻止盘符候选。"""
    scan = [{"device": "/dev/sdz", "type": "sat"}]
    _set_scan(monkeypatch, scan)
    _fake_idents(monkeypatch, {"/dev/sdz": _ident("SOMEONE-ELSE",
                                                  cap=JM_WIN32_SIZE)})
    cands, si = smart.resolve_smart_device(
        expected_serial=JM_BOX_SER, expected_capacity_bytes=JM_WIN32_SIZE,
        expected_model=JM_BOX_MODEL, expected_interface="USB", letter="E")
    assert cands[0]["device"] == "E:"
    assert si["mapped_by"] == "letter_direct"
    assert si.get("identity_risk") != "target_not_found"


# ---------------------------------------------------------------- scan 缓存短 TTL + 换盘强制刷新

def test_scan_cache_ttl_expiry_rescans(monkeypatch) -> None:
    """缓存新鲜 → 不重扫；超过 _SCAN_TTL → 重扫拿新设备表（换盘后旧表
    最多活 15 秒，绝不进程内永久绑定）。"""
    monkeypatch.setattr(smart, "_run_cmd", lambda cmd: type(
        "P", (), {"stdout": json.dumps({"devices": [
            {"name": "/dev/sdz", "type": "sat"}]}), "returncode": 0})())
    monkeypatch.setattr(smart, "_SCAN_CACHE",
                        [{"device": "/dev/sda", "type": "sat"}])
    monkeypatch.setattr(smart, "_SCAN_TS", time.monotonic())   # 新鲜
    assert smart.scan_devices() == [{"device": "/dev/sda", "type": "sat"}]
    monkeypatch.setattr(smart, "_SCAN_TS",
                        time.monotonic() - smart._SCAN_TTL - 1)  # 过期
    assert smart.scan_devices() == [{"device": "/dev/sdz", "type": "sat"}]


def test_scan_cache_refresh_on_identity_miss(monkeypatch) -> None:
    """身份读取全军覆没（扫描表里全是别的盘，疑似换盘前旧表）→ 强制刷新
    重扫一次，换盘后的新表里找到目标。"""
    calls: list = []

    def fake_run(cmd):
        calls.append(cmd)
        # 第一次：旧表（没有目标盘）；刷新后：新表（目标已插入）
        entries = ([{"name": "/dev/sdx", "type": "sat"}] if len(calls) <= 1
                   else [{"name": "/dev/sdx", "type": "sat"},
                         {"name": "/dev/sdy", "type": "sat"}])
        return type("P", (), {"stdout": json.dumps({"devices": entries}),
                              "returncode": 0})()

    monkeypatch.setattr(smart, "_run_cmd", fake_run)
    idents = {"/dev/sdx": _ident("OLD-DISK"),
              "/dev/sdy": _ident(EXP)}
    monkeypatch.setattr(smart, "read_device_identity",
                        lambda dev, sug=None: idents.get(dev))
    monkeypatch.setattr(smart, "_SCAN_CACHE", None)
    monkeypatch.setattr(smart, "_SCAN_TS", 0.0)
    cands, si = smart.resolve_smart_device(expected_serial=EXP)
    assert len(calls) == 2           # 第二次 --scan（强制刷新）
    assert cands[0]["device"] == "/dev/sdy"
    assert si["mapped_by"] == "serial_match"
    smart.reset_scan_cache()


def test_scan_cache_no_refresh_when_target_found(monkeypatch) -> None:
    """正常命中（序列号精确匹配）→ 不触发强制刷新，一次扫描就够。"""
    calls: list = []

    def fake_run(cmd):
        calls.append(cmd)
        return type("P", (), {"stdout": json.dumps({"devices": [
            {"name": "/dev/sda", "type": "sat"}]}), "returncode": 0})()

    monkeypatch.setattr(smart, "_run_cmd", fake_run)
    monkeypatch.setattr(smart, "read_device_identity",
                        lambda dev, sug=None: _ident(EXP))
    monkeypatch.setattr(smart, "_SCAN_CACHE", None)
    monkeypatch.setattr(smart, "_SCAN_TS", 0.0)
    cands, _si = smart.resolve_smart_device(expected_serial=EXP)
    assert len(calls) == 1           # --scan-open -j 一次即命中，不重扫
    assert cands[0]["device"] == "/dev/sda"
    smart.reset_scan_cache()


def test_letter_not_bound_into_identity(monkeypatch) -> None:
    """盘符只作当场查询线索：resolve 的盘符候选不携带任何序列号/持久绑定
    字段（无 identity_serial、无 note 之外的绑定信息）。"""
    _set_scan(monkeypatch, _DIAG_SCAN)
    _fake_idents(monkeypatch, _DIAG_IDENTS)
    cands, si = smart.resolve_smart_device(
        expected_serial=JM_BOX_SER, expected_capacity_bytes=JM_WIN32_SIZE,
        expected_model=JM_BOX_MODEL, expected_interface="USB", letter="E")
    lt = next(c for c in cands if c["source"] == "letter")
    assert lt["device"] == "E:"
    assert "identity_serial" not in lt
    assert "serial" not in lt and "model" not in lt
    # scan_info.candidates 投影同样不带盘符→序列号绑定
    proj = next(c for c in si["candidates"] if c["source"] == "letter")
    assert "identity_serial" not in proj


# ---------------------------------------------------------------- 端到端（假 scan / 假 smartctl）

def test_e2e_probe_box_id_verified(monkeypatch) -> None:
    """端到端复刻用户场景：probe_path_win（盒 ID 探针序列号 + 4T 容量）
    → identity_verified=True、真盘序列号被采纳。"""

    class _FakePath(str):
        def exists(self):
            return True

        def resolve(self):
            return self

    ps = json.loads(_PS_JSON)
    ps["disk"]["serial"] = BOX_ID
    ps["disk"]["model"] = "TOSHIBA EXTERNAL_USB"
    ps["disk"]["size"] = CAP_4T
    ps["volume"]["size"] = CAP_4T
    monkeypatch.setattr(pw, "Path", _FakePath)
    monkeypatch.setattr(pw, "run_powershell",
                        lambda script, timeout=60: json.dumps(ps))
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"},
                            {"device": "/dev/sdb", "type": "sat"}])
    _fake_idents(monkeypatch, {
        "/dev/sda": _ident(REAL_4T, model="TOSHIBA MG09", cap=CAP_4T),
        "/dev/sdb": _ident(REAL_2T, model="WD RED", cap=CAP_2T)})
    raw = json.dumps({"serial_number": REAL_4T,
                      "model_name": "TOSHIBA MG09",
                      "user_capacity": {"bytes": CAP_4T}})
    monkeypatch.setattr(smart, "read_smart_verbose",
                        lambda dev=None, *, devices=None, **kw: {
                            "ok": True, "raw": raw, "device": devices[0]["device"],
                            "device_type": "sat", "exit_status": 0,
                            "reason": None, "message": None,
                            "raw_excerpt": raw, "attempts": [],
                            "device_candidates": [devices[0]["device"]],
                            "scan_info": {"device_used":
                                          devices[0]["device"]}})
    _volume, info = pw.probe_path_win("E:\\", smartctl=True)
    rsi = info.smart_scan_info or {}
    # 有盘符（E:）→ 盘符直读优先：mapped_by=letter_direct，读到的就是
    # 目标盘真序列号（读后校验：USB 总线 + 容量吻合均通过）
    assert rsi["mapped_by"] == "letter_direct"
    assert rsi["device_used"] == "E:"
    assert info.identity_verified is True
    assert info.disk_serial == REAL_4T          # 真序列号被采纳
    assert info.serial_source == "smartctl"
    assert info.physical_serial == REAL_4T
    assert "盘符" in (rsi.get("identity_reason") or "")
    assert info.identity_reason and "盘符" in info.identity_reason
