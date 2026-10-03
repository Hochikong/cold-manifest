"""身份审计（PR-C）测试：三类告警 + 容错 + CLI 退出码。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cold_manifest.catalog import catalog_path, ensure_disk, ensure_volume, \
    register_snapshot
from cold_manifest.cli import main
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.identity_audit import audit_identities

D1 = "SNAAA111"
D2 = "SNBBB222"
V1 = f"{D1}_P1"
V2 = f"{D2}_P1"
SID_A1 = f"{V1}/20260101T000000Z"
SID_A2 = f"{V1}/20260201T000000Z"
SID_A3 = f"{V1}/20260301T000000Z"
SID_B1 = f"{V2}/20260101T000000Z"


def _raw_json(serial: str) -> str:
    return json.dumps({"serial_number": serial, "model_name": "TOSHIBA HDWD120"})


def _make_snapshot_db(data_root: Path, sid: str, meta: dict) -> None:
    p = data_root / sid.split("/")[0] / sid.split("/")[1] / "snapshot.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    init_snapshot(conn)
    conn.execute("INSERT INTO entries(entry_id, parent_id, path, name, depth, type,"
                 " size_bytes) VALUES(1, 0, '.', '', 0, 'dir', NULL)")
    for k, v in meta.items():
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?)", (k, v))
    conn.execute("INSERT INTO meta(key, value) VALUES('status', 'sealed')")
    conn.commit()
    conn.close()


def _make_root(tmp_path: Path, disks: "dict[str, list[str]]",
               snaps: "dict[str, dict]", name: str = "data") -> Path:
    """disks: disk_id → volume_ids；snaps: sid → meta dict。"""
    data_root = tmp_path / name
    data_root.mkdir(exist_ok=True)
    conn = sqlite3.connect(catalog_path(data_root))
    init_catalog(conn)
    for disk_id, vols in disks.items():
        ensure_disk(conn, disk_id)
        for vid in vols:
            ensure_volume(conn, vid, disk_id)
    for i, (sid, meta) in enumerate(sorted(snaps.items())):
        register_snapshot(conn, sid, sid.split("/")[0], status="sealed",
                          host_path="/x",
                          collected_at=f"2026-01-{i + 1:02d}T00:00:00Z")
    conn.commit()
    conn.close()
    for sid, meta in snaps.items():
        _make_snapshot_db(data_root, sid, meta)
    return data_root


# ---------------------------------------------------------------- ① intra drift

def test_intra_snapshot_serial_drift(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {D1: [V1]}, {
        SID_A1: {"disk_serial": D1, "physical_serial": D1,
                 "smart_raw_json": _raw_json("SNXXX999")},   # 与 meta 不一致
    })
    res = audit_identities(root)
    a = next(x for x in res["alerts"]
             if x["type"] == "intra_snapshot_serial_drift")
    assert a["severity"] == "high"
    assert a["disk_id"] == D1 and a["snapshot_id"] == SID_A1
    assert a["serials"]["physical_serial"] == D1
    assert a["serials"]["smart_raw"] == "SNXXX999"
    assert a["suggestion"]
    assert res["summary"]["high"] == 1  # 单快照不再触发跨快照 volatility
    assert res["affected_disk_ids"] == [D1]


def test_intra_drift_physical_vs_smart_raw(tmp_path: Path) -> None:
    """真矛盾：physical_serial != smart_raw → 1 条 HIGH intra。"""
    root = _make_root(tmp_path, {D1: [V1]}, {
        SID_A1: {"physical_serial": "AAAA1111",
                 "smart_raw_json": _raw_json("BBBB2222")},
    })
    res = audit_identities(root)
    kinds = [a["type"] for a in res["alerts"]]
    assert kinds.count("intra_snapshot_serial_drift") == 1
    assert res["summary"]["high"] == 1


# ------------------------------------------------ USB 桥正常形态（用户回归）

def test_usb_bridge_form_no_high(tmp_path: Path) -> None:
    """用户真实场景回归：disk_serial=桥上报 ID，physical_serial==smart_raw
    （真盘序列号，读取自洽）→ 不产生 HIGH，只记 notes。"""
    meta = {"disk_serial": "0025_3844_51B1_4EFD.",
            "physical_serial": "WW67MJJE",
            "smart_raw_json": _raw_json("WW67MJJE")}
    root = _make_root(tmp_path, {"0025_3844_51B1_4EFD": [V1]}, {SID_A1: meta})
    res = audit_identities(root)
    assert res["summary"]["high"] == 0
    assert not any(a["severity"] == "high" for a in res["alerts"])
    assert any(n["issue"] == "usb_bridge_serial_form" for n in res["notes"])


def test_usb_bridge_form_three_volumes_no_high(tmp_path: Path) -> None:
    """用户实测三条记录同形 → high=0。"""
    disks = {
        "0025_3844_51B1_4EFD": ("0025_3844_51B1_4EFD._P1", "WW67MJJE"),
        "20170523006951F": ("20170523006951F_P1", "57N4TB2PT"),
        "20260123004775F": ("20260123004775F_P2", "16NDT0O1T"),
    }
    snaps: "dict[str, dict]" = {}
    vols: "dict[str, list[str]]" = {}
    for disk_id, (vid, serial) in disks.items():
        vols[disk_id] = [vid]
        snaps[f"{vid}/20261002T151337Z"] = {
            "disk_serial": f"{disk_id}.", "physical_serial": serial,
            "smart_raw_json": _raw_json(serial)}
    root = _make_root(tmp_path, vols, snaps)
    res = audit_identities(root)
    assert res["summary"]["high"] == 0
    assert all(a["severity"] != "high" for a in res["alerts"])


# ------------------------------------------------- 跨盘序列号别名（真问题）

def test_cross_disk_serial_alias_bridge_form(tmp_path: Path) -> None:
    """用户真实形态：盘 A 的 disk_serial 是**另一块盘 B**的序列号
    （设备定位错位写入），smart 侧自洽 → 旧规则当桥形态只记 note，
    新规则必须报 1 条 HIGH cross_disk_serial_alias，且不与 intra 重复。"""
    alias = "0025_3844_51B1_4EFD."          # 盘 B（Samsung NVMe）的序列号
    root = _make_root(tmp_path, {
        "WW67MJJE": ["WW67MJJE_P1"],        # 盘 A：真序列号 WW67MJJE
        alias: [f"{alias}_P1"],             # 盘 B：持有 0025_…
    }, {
        "WW67MJJE_P1/20260101T000000Z": {
            "disk_serial": alias, "physical_serial": "WW67MJJE",
            "smart_raw_json": _raw_json("WW67MJJE")},
    })
    res = audit_identities(root)
    crosses = [a for a in res["alerts"]
               if a["type"] == "cross_disk_serial_alias"]
    assert len(crosses) == 1
    a = crosses[0]
    assert a["severity"] == "high"
    assert a["disk_id"] == "WW67MJJE"
    assert a["snapshot_id"] == "WW67MJJE_P1/20260101T000000Z"
    assert a["serials"] == {"disk_serial": alias}
    assert a["alias_disk_ids"] == [alias]
    assert a["suggestion"]
    # 不与 intra 重复、桥形态 note 被抑制
    assert not any(x["type"] == "intra_snapshot_serial_drift"
                   for x in res["alerts"])
    assert not any(n["issue"] == "usb_bridge_serial_form"
                   for n in res["notes"])
    assert res["summary"]["high"] == 1


def test_cross_disk_no_alias_single_disk(tmp_path: Path) -> None:
    """单盘、没有别的盘持有该序列号 → 不报 cross_disk（保持桥形态 note）。"""
    meta = {"disk_serial": "0025_3844_51B1_4EFD.",
            "physical_serial": "WW67MJJE",
            "smart_raw_json": _raw_json("WW67MJJE")}
    root = _make_root(tmp_path, {"0025_3844_51B1_4EFD": [V1]},
                      {SID_A1: meta})
    res = audit_identities(root)
    assert not any(a["type"] == "cross_disk_serial_alias"
                   for a in res["alerts"])
    assert res["summary"]["high"] == 0
    assert any(n["issue"] == "usb_bridge_serial_form" for n in res["notes"])


def test_cross_disk_distinct_serials_no_alerts(tmp_path: Path) -> None:
    """两盘序列号互不相同 → 0 告警。"""
    root = _make_root(tmp_path, {D1: [V1], D2: [V2]}, {
        SID_A1: {"disk_serial": D1, "physical_serial": D1,
                 "smart_raw_json": _raw_json(D1)},
        SID_B1: {"disk_serial": D2, "physical_serial": D2,
                 "smart_raw_json": _raw_json(D2)},
    })
    res = audit_identities(root)
    assert res["alerts"] == []
    assert res["summary"] == {"high": 0, "medium": 0, "low": 0}


# ---------------------------------------------------------------- ② volatility

def test_inter_snapshot_serial_volatility(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {D1: [V1]}, {
        SID_A1: {"disk_serial": D1, "serial_source": "smartctl"},
        SID_A2: {"disk_serial": "SNYYY888", "serial_source": "smartctl"},
    })
    res = audit_identities(root)
    a = next(x for x in res["alerts"]
             if x["type"] == "inter_snapshot_serial_volatility")
    assert a["severity"] == "high"
    assert a["volume_id"] == V1
    assert a["snapshot_ids"] == [SID_A1, SID_A2]
    assert a["field"] == "disk_serial"
    assert a["serials"]["disk_serial"][D1] == [SID_A1]
    assert res["summary"]["high"] >= 1


def test_volatility_real_swap_single_alert(tmp_path: Path) -> None:
    """真串盘：同卷两快照 smart_raw 不同 → 恰好 1 条 HIGH volatility。"""
    root = _make_root(tmp_path, {D1: [V1]}, {
        SID_A1: {"smart_raw_json": _raw_json("AAAA1111")},
        SID_A2: {"smart_raw_json": _raw_json("BBBB2222")},
    })
    res = audit_identities(root)
    vols = [a for a in res["alerts"]
            if a["type"] == "inter_snapshot_serial_volatility"]
    assert len(vols) == 1
    assert vols[0]["severity"] == "high"
    assert vols[0]["field"] == "smart_raw"
    assert res["summary"]["high"] == 1


def test_volatility_not_mixed_within_single_snapshot(tmp_path: Path) -> None:
    """单快照内不同字段取值不同（桥形态）不得触发跨快照 volatility。"""
    root = _make_root(tmp_path, {D1: [V1]}, {
        SID_A1: {"disk_serial": "BRIDGE1", "physical_serial": "REAL1",
                 "smart_raw_json": _raw_json("REAL1")},
        SID_A2: {"disk_serial": "BRIDGE1", "physical_serial": "REAL1",
                 "smart_raw_json": _raw_json("REAL1")},
    })
    res = audit_identities(root)
    assert res["summary"]["high"] == 0
    assert not any(a["type"] == "inter_snapshot_serial_volatility"
                   for a in res["alerts"])


def test_consistent_serials_no_alerts(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {D1: [V1]}, {
        SID_A1: {"disk_serial": D1, "smart_raw_json": _raw_json(D1)},
        SID_A2: {"disk_serial": D1, "smart_raw_json": _raw_json(D1)},
    })
    res = audit_identities(root)
    assert res["alerts"] == []
    assert res["summary"] == {"high": 0, "medium": 0, "low": 0}


# ---------------------------------------------------------------- ③ fallback

def test_high_fallback_ratio(tmp_path: Path) -> None:
    scan_info = json.dumps({
        "candidates": [{"device": "/dev/sdb", "type": "", "source": "fallback-sd"}],
        "device_used": "/dev/sdb", "mapped_from_scan": False,
    })
    root = _make_root(tmp_path, {D1: [V1]}, {
        SID_A1: {"disk_serial": D1, "smart_scan_info_json": scan_info},
        SID_A2: {"disk_serial": D1, "serial_source": "volume_serial_fallback"},
    })
    res = audit_identities(root)
    a = next(x for x in res["alerts"] if x["type"] == "high_fallback_ratio")
    assert a["severity"] == "medium"
    assert a["disk_id"] == D1
    assert a["fallback_count"] == 2 and a["snapshot_count"] == 2
    assert res["summary"] == {"high": 0, "medium": 1, "low": 0}


def test_no_fallback_alert_when_minority(tmp_path: Path) -> None:
    scan_info = json.dumps({
        "candidates": [{"device": "/dev/sdb", "type": "", "source": "scan"}],
        "device_used": "/dev/sdb", "mapped_from_scan": True,
    })
    root = _make_root(tmp_path, {D1: [V1]}, {
        SID_A1: {"disk_serial": D1, "smart_scan_info_json": scan_info},
        SID_A2: {"disk_serial": D1, "serial_source": "volume_serial_fallback"},
    })
    res = audit_identities(root)
    # 未过半 → 只降为 low 信息性提示，不产生 high/medium
    assert res["summary"]["high"] == 0 and res["summary"]["medium"] == 0
    assert res["summary"]["low"] == 1
    a = res["alerts"][0]
    assert a["type"] == "fallback_minority" and a["severity"] == "low"


# ---------------------------------------------------------------- 容错

def test_corrupt_and_unsealed_snapshots_become_warnings(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {D1: [V1]}, {SID_A1: {"disk_serial": D1}})
    # 损坏库
    bad = root / "X" / "bad" / "snapshot.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a sqlite file" * 100)
    conn = sqlite3.connect(catalog_path(root))
    conn.execute("INSERT INTO snapshots(snapshot_id, volume_id, status, host_path)"
                 " VALUES('X/bad', ?, 'sealed', '/x')", (V1,))
    conn.commit()
    conn.close()
    res = audit_identities(root)
    assert res["alerts"] == []
    assert any(w["snapshot_id"] == "X/bad" and w["issue"] == "snapshot_db_error"
               for w in res["warnings"])
    assert res["summary"] == {"high": 0, "medium": 0, "low": 0}


def test_unsealed_snapshot_skipped(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {D1: [V1]}, {SID_A1: {"disk_serial": D1}})
    # 把 status 改成 incomplete
    p = root / SID_A1.split("/")[0] / SID_A1.split("/")[1] / "snapshot.db"
    conn = sqlite3.connect(p)
    conn.execute("UPDATE meta SET value='incomplete' WHERE key='status'")
    conn.commit()
    conn.close()
    res = audit_identities(root)
    assert res["alerts"] == []
    assert any(w["issue"] == "not_sealed" for w in res["warnings"])


def test_missing_db_becomes_warning(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {D1: [V1]}, {SID_A1: {"disk_serial": D1}})
    (root / SID_A1.split("/")[0] / SID_A1.split("/")[1] / "snapshot.db").unlink()
    res = audit_identities(root)
    assert any(w["issue"] == "snapshot_db_missing" for w in res["warnings"])


def test_missing_data_root(tmp_path: Path) -> None:
    with pytest.raises(Exception):
        audit_identities(tmp_path / "nope")


# ---------------------------------------------------------------- CLI

def test_cli_exit_codes(tmp_path: Path, capsys) -> None:
    # 有 high 告警 → 1
    root = _make_root(tmp_path, {D1: [V1]}, {
        SID_A1: {"disk_serial": D1, "smart_raw_json": _raw_json("SNXXX999")},
    })
    assert main(["identity-check", "--data-root", str(root)]) == 1
    out = capsys.readouterr().out
    assert "高危" in out and "同快照序列号自相矛盾" in out

    # 只有 low（未过半回退提示）→ 退出码 0
    scan_info = json.dumps({
        "candidates": [{"device": "/dev/sdb", "type": "", "source": "scan"}],
        "device_used": "/dev/sdb", "mapped_from_scan": True,
    })
    root_low = _make_root(tmp_path, {D2: [V2]}, {
        SID_B1: {"disk_serial": D2, "smart_scan_info_json": scan_info,
                 "serial_source": "volume_serial_fallback"},
        f"{V2}/20260201T000000Z": {"disk_serial": D2},
    }, name="datalow")
    assert main(["identity-check", "--data-root", str(root_low)]) == 0
    out_low = capsys.readouterr().out
    assert "低危" in out_low and "high=0" in out_low

    # 无告警 → 0
    root2 = _make_root(tmp_path, {D1: [V1]}, {SID_A2: {"disk_serial": D1}},
                       name="data2")
    assert main(["identity-check", "--data-root", str(root2)]) == 0
    assert "未发现身份疑点" in capsys.readouterr().out

    # 数据根错误 → 2
    assert main(["identity-check", "--data-root", str(tmp_path / "nope")]) == 2


def test_cli_json_output(tmp_path: Path, capsys) -> None:
    root = _make_root(tmp_path, {D1: [V1]}, {
        SID_A1: {"disk_serial": D1, "smart_raw_json": _raw_json("SNXXX999")},
    })
    assert main(["identity-check", "--data-root", str(root), "--json"]) == 1
    body = json.loads(capsys.readouterr().out)
    assert body["summary"] == {"high": 1, "medium": 0, "low": 0}
    assert body["alerts"][0]["type"] == "intra_snapshot_serial_drift"


# ------------------------------------------ disk_identity_status 原因透出

from cold_manifest.identity_audit import disk_identity_status  # noqa: E402


def _disk_with_verified(tmp_path: Path, verified: int,
                        snaps: "dict[str, dict]",
                        name: str = "datav") -> "tuple[Path, sqlite3.Connection]":
    root = _make_root(tmp_path, {D1: [V1]}, snaps, name=name)
    conn = sqlite3.connect(catalog_path(root))
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(disks)")}
    if "identity_verified" not in cols:
        conn.execute("ALTER TABLE disks ADD COLUMN identity_verified INTEGER")
    conn.execute("UPDATE disks SET identity_verified=?", (verified,))
    conn.commit()
    return root, conn


def test_status_conflict_reason_from_latest_meta(tmp_path: Path) -> None:
    reason = ("探针读到的是硬盘盒上报的 ID（X）；SMART 已按容量唯一定位并读到"
              "真盘序列号 Y")
    root, conn = _disk_with_verified(tmp_path, 0, {
        SID_A1: {"disk_serial": D1, "identity_reason": "旧的原因"},
        SID_A2: {"disk_serial": D1, "identity_reason": reason},
    })
    try:
        status, why = disk_identity_status(conn, root, D1)
        assert status == "conflict"
        assert reason in why
        assert why.startswith("最近一次身份校验未通过：")
    finally:
        conn.close()


def test_status_conflict_generic_without_meta_reason(tmp_path: Path) -> None:
    root, conn = _disk_with_verified(tmp_path, 0, {SID_A1: {"disk_serial": D1}})
    try:
        status, why = disk_identity_status(conn, root, D1)
        assert status == "conflict"
        assert "未通过" in why and "identity_verified=0" in why
    finally:
        conn.close()


def test_status_unverified_reason_from_warnings(tmp_path: Path) -> None:
    scan_info = json.dumps({
        "candidates": [{"device": "/dev/sdb", "type": "",
                        "source": "fallback-sd"}],
        "device_used": "/dev/sdb", "mapped_from_scan": False,
    })
    root = _make_root(tmp_path, {D1: [V1]}, {
        SID_A1: {"disk_serial": D1, "smart_scan_info_json": scan_info,
                 "identity_warnings_json": json.dumps(["…原因A…"])},
    })
    conn = sqlite3.connect(catalog_path(root))
    conn.row_factory = sqlite3.Row
    try:
        status, why = disk_identity_status(conn, root, D1)
        assert status == "unverified"
        assert "原因A" in why
    finally:
        conn.close()


def test_status_unknown_new_wording(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {D1: [V1]}, {SID_A1: {"disk_serial": D1}})
    conn = sqlite3.connect(catalog_path(root))
    conn.row_factory = sqlite3.Row
    try:
        status, why = disk_identity_status(conn, root, D1)
        assert status == "unknown"
        assert "开启身份校验" not in why
        assert "自动核对" in why
    finally:
        conn.close()
