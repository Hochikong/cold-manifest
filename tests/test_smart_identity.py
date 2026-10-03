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
    # 双方都有序列号但不一致 → 0（另一块盘）
    assert smart._score_identity_match(_ident(OTHER), EXP, 1000, "M") == 0
    # serial 缺失：容量≤5% + 型号 → 80
    assert smart._score_identity_match(_ident(None), EXP, 1000, "M") == 80
    # 容量≤20% + 型号 → 50；仅容量 → 20；全无 → 0
    assert smart._score_identity_match(_ident(None, cap=1100), EXP, 1000, "M") == 50
    assert smart._score_identity_match(_ident(None, model=None), EXP, 1000, None) == 20
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
    assert si["mapped_by"] == "weak_match"


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
