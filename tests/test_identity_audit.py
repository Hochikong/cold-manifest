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
    assert a["serials"]["meta.disk_serial"] == D1
    assert a["serials"]["smart_raw.serial_number"] == "SNXXX999"
    assert a["suggestion"]
    assert res["summary"]["high"] == 2  # intra drift + volatility 均成立
    assert res["affected_disk_ids"] == [D1]


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
    assert a["serials"][SID_A1]["disk_serial"] == D1
    assert res["summary"]["high"] >= 1


def test_consistent_serials_no_alerts(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {D1: [V1]}, {
        SID_A1: {"disk_serial": D1, "smart_raw_json": _raw_json(D1)},
        SID_A2: {"disk_serial": D1, "smart_raw_json": _raw_json(D1)},
    })
    res = audit_identities(root)
    assert res["alerts"] == []
    assert res["summary"] == {"high": 0, "medium": 0}


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
    assert res["summary"] == {"high": 0, "medium": 1}


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
    assert [a["type"] for a in res["alerts"]] == []


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
    assert res["summary"] == {"high": 0, "medium": 0}


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
    # 同一夹具同时命中 intra drift 与 volatility（见上）
    assert "高危" in out and "同快照序列号自相矛盾" in out

    # 无告警 → 0
    capsys.readouterr()
    root2 = _make_root(tmp_path, {D2: [V2]}, {SID_B1: {"disk_serial": D2}},
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
    assert body["summary"]["high"] == 2
    assert body["alerts"][0]["type"] == "intra_snapshot_serial_drift"
