"""PR-B：SMART 设备按序列号校验身份（resolve_smart_device / probe 条件覆盖）。

背景：旧实现把 Win32_DiskDrive.Index 当 smartctl --scan 行序下标，全程无
序列号校验，同型号同容量多盘时会把另一块盘的 SMART（乃至序列号）写进
meta/catalog。本文件覆盖：身份打分排序、歧义检测、回退候选标记、probe
序列号覆盖的新语义（验证过才覆盖，否则只补全/保留 probe 值）。
"""

from __future__ import annotations

import json

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
    不追加任何未验证回退候选（已证明不是，而非无法证明）。"""
    _set_scan(monkeypatch, [{"device": "/dev/sda", "type": "sat"},
                            {"device": "/dev/sdb", "type": "sat"}])
    _fake_idents(monkeypatch, {"/dev/sda": _ident("SOMEONE-ELSE"),
                               "/dev/sdb": _ident("OTHER-DISK")})
    cands, si = smart.resolve_smart_device(
        expected_serial=EXP, disk_index=0, letter="E")
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
                            "scan_info": {}})
    _volume, info = pw.probe_path_win("E:\\", smartctl=True)
    assert info.identity_verified is True
    assert info.disk_serial == REAL_4T          # 真序列号被采纳
    assert info.serial_source == "smartctl"
    assert info.physical_serial == REAL_4T
    rsi = info.smart_scan_info or {}
    assert rsi["mapped_by"] == "unique_capacity_match"
    assert BOX_ID in (rsi.get("identity_reason") or "")
    assert REAL_4T in (rsi.get("identity_reason") or "")
    assert info.identity_reason and BOX_ID in info.identity_reason
    assert any(BOX_ID in w and REAL_4T in w
               for w in (info.identity_warnings or []))
