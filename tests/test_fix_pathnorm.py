"""path_norm 修复链路测试：采集/导入同规则、fix-pathnorm 回填（幂等/只补空行/跳过未封库）。"""

import csv
import sqlite3
from pathlib import Path

import pytest

from cold_manifest.cli import main
from cold_manifest.collect import collect_volume
from cold_manifest.import_legacy import import_snapshot
from cold_manifest.paths import normalize_path
from cold_manifest.probe import DiskInfo, VolumeInfo
from cold_manifest.schema import SNAPSHOT_DDL


# ---------------------------------------------------------------- 夹具

def _fake_probe(monkeypatch, mount: str = "/mnt/fake") -> None:
    vol = VolumeInfo(
        filesystem="ext4", label="FAKELBL", volume_serial_hex="abcd-1234",
        partition_uuid="1111-2222", partition_index=1, partition_table_type="GPT",
        capacity_bytes=10_000_000_000, free_bytes=9_000_000_000,
        mount_point=mount, device_path="/dev/sdb1",
    )
    disk = DiskInfo(
        physical_model="Fake Disk 5000", physical_serial="SERFAKE123",
        disk_serial="SERFAKE123", serial_source="probe",
        bridge_model="", interface_type="USB", capacity_bytes=20_000_000_000,
        firmware="fw1", smart_status="unavailable",
    )

    def probe(path, *, manual_serial=None, smartctl=True):
        return vol, disk

    monkeypatch.setattr("cold_manifest.collect.probe_path", probe)


# 含中文与大小写混合的相对路径（两种来源各跑一遍，规则必须一致）
PATHS = [
    "Docs/ReadMe.TXT",
    "docs/照片/IMG_0001.JPG",
    "Case/Upper/lower.txt",
    "混合/大小写Mix.bin",
]


def _make_tree(root: Path) -> None:
    for rel in PATHS:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 10)


def _entries(db: Path) -> "dict[str, str]":
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT path, path_norm FROM entries WHERE type='file'").fetchall()
        return dict(rows)
    finally:
        conn.close()


def _make_legacy_dir(root: Path) -> Path:
    d = root / "legacy_snap"
    d.mkdir()
    (d / "metadata.csv").write_text(
        "metric_name,value\n"
        "volume_id,TESTVOL_P0\n"
        "disk_serial,SER123\n"
        "partition_index,1\n"
        "model,Fake Disk\n"
        "disk_capacity_bytes,1000000\n"
        "partition_capacity_bytes,999999\n"
        "filesystem,NTFS\n"
        "volume_label,LBL\n"
        "interface_type,USB\n"
        "collect_time_utc,2026-05-04T12:34:16Z\n"
        "collector_version,0.1.0\n",
        encoding="utf-8",
    )
    with (d / "tree.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["volume_id", "relative_path", "entry_name", "entry_type",
                    "size_bytes", "modified_utc"])
        for rel in PATHS:
            w.writerow(["TESTVOL_P0", rel, rel.rsplit("/", 1)[-1], "file",
                        "10", "2026-01-02T03:04:05Z"])
    with (d / "warnings.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stage", "path", "warning_type", "detail"])
    return d


# ---------------------------------------------------------------- 采集侧

def test_collect_writes_path_norm(tmp_path: Path, monkeypatch) -> None:
    """采集出的快照所有条目 path_norm 非空且 == normalize_path(path)。"""
    _fake_probe(monkeypatch)
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    data_root = tmp_path / "data"
    result = collect_volume(scan_root, data_root=data_root)
    entries = _entries(data_root / f"{result.snapshot_id}/snapshot.db")
    assert set(entries) >= set(PATHS)
    for p, pn in entries.items():
        assert pn, f"path_norm 为空：{p}"
        assert pn == normalize_path(p)


def test_import_and_collect_same_rule(tmp_path: Path, monkeypatch) -> None:
    """同一路径两种来源（采集 / legacy 导入）path_norm 完全相同。"""
    _fake_probe(monkeypatch)
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    data_root = tmp_path / "data"
    result = collect_volume(scan_root, data_root=data_root)
    collected = _entries(data_root / f"{result.snapshot_id}/snapshot.db")

    import_snapshot(_make_legacy_dir(tmp_path), data_root=data_root)
    imported = _entries(data_root / "TESTVOL_P0" / "20260504T123416Z" / "snapshot.db")

    for rel in PATHS:
        assert rel in collected and rel in imported
        assert collected[rel] == imported[rel] == normalize_path(rel)


# ---------------------------------------------------------------- fix-pathnorm

def _mk_snapshot(db: Path, *, sealed: bool) -> None:
    """手工快照库：2 行 path_norm 缺失 + 1 行已有值（不得被覆盖）。"""
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(SNAPSHOT_DDL)
    conn.executemany(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type,"
        " path_norm) VALUES(?,?,?,?,?,?,?)",
        [
            (1, 0, ".", "", 0, "dir", "."),          # 已有值：绝不覆盖
            (2, 1, "Docs/A.TXT", "A.TXT", 1, "file", None),   # 缺失 → 补
            (3, 1, "中文/名.txt", "名.txt", 1, "file", ""),    # 空串 → 补
        ],
    )
    if sealed:
        conn.execute("INSERT INTO meta(key, value) VALUES('status', 'sealed')")
    conn.commit()
    conn.close()


def test_fix_pathnorm_fills_only_empty(tmp_path: Path, capsys) -> None:
    db = tmp_path / "VOL" / "20260101T000000Z" / "snapshot.db"
    _mk_snapshot(db, sealed=True)
    sid = "VOL/20260101T000000Z"

    rc = main(["fix-pathnorm", sid, "--data-root", str(tmp_path)])
    assert rc == 0
    assert "补写 2 行" in capsys.readouterr().out

    conn = sqlite3.connect(str(db))
    rows = dict(conn.execute("SELECT entry_id, path_norm FROM entries"))
    conn.close()
    assert rows[1] == "."                      # 已有值不动
    assert rows[2] == normalize_path("Docs/A.TXT")
    assert rows[3] == normalize_path("中文/名.txt")


def test_fix_pathnorm_idempotent(tmp_path: Path, capsys) -> None:
    db = tmp_path / "VOL" / "20260101T000000Z" / "snapshot.db"
    _mk_snapshot(db, sealed=True)
    sid = "VOL/20260101T000000Z"

    assert main(["fix-pathnorm", sid, "--data-root", str(tmp_path)]) == 0
    capsys.readouterr()
    assert main(["fix-pathnorm", sid, "--data-root", str(tmp_path)]) == 0
    assert "无需补写" in capsys.readouterr().out


def test_fix_pathnorm_skips_unsealed(tmp_path: Path, capsys) -> None:
    """未封库的库跳过并告警，绝不写入。"""
    db = tmp_path / "VOL" / "20260101T000000Z" / "snapshot.db"
    _mk_snapshot(db, sealed=False)
    sid = "VOL/20260101T000000Z"

    rc = main(["fix-pathnorm", sid, "--data-root", str(tmp_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "未封库" in captured.err

    conn = sqlite3.connect(str(db))
    norms = conn.execute("SELECT path_norm FROM entries WHERE entry_id IN (2,3)"
                         ).fetchall()
    conn.close()
    assert norms == [(None,), ("",)]  # 原样未写


def test_fix_pathnorm_all(tmp_path: Path, capsys) -> None:
    _mk_snapshot(tmp_path / "VOL" / "20260101T000000Z" / "snapshot.db", sealed=True)
    _mk_snapshot(tmp_path / "VOL" / "20260102T000000Z" / "snapshot.db", sealed=True)
    rc = main(["fix-pathnorm", "--all", "--data-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("补写 2 行") == 2
    assert "合计补写：4 行" in out


def test_fix_pathnorm_missing_snapshot(tmp_path: Path) -> None:
    rc = main(["fix-pathnorm", "NOPE/20260101T000000Z", "--data-root", str(tmp_path)])
    assert rc == 2


# ---------------------------------------------------------------- snapshot_id 反斜杠归一

def test_snapshot_id_backslash_equivalent(tmp_path: Path, capsys) -> None:
    """Windows 用户自然输入反斜杠（VOL\\TS）与正斜杠等价，不再被 volume_id 校验拒绝。"""
    db = tmp_path / "VOL" / "20260101T000000Z" / "snapshot.db"
    _mk_snapshot(db, sealed=True)
    sid = "VOL/20260101T000000Z"

    assert main(["fix-pathnorm", "VOL\\20260101T000000Z",
                 "--data-root", str(tmp_path)]) == 0
    assert "补写 2 行" in capsys.readouterr().out

    # 归一后再跑一次正斜杠写法：结果与反斜杠写法完全一致（幂等）
    assert main(["fix-pathnorm", sid, "--data-root", str(tmp_path)]) == 0
    assert "无需补写" in capsys.readouterr().out


def test_snapshot_id_backslash_real_path_untouched(tmp_path: Path, capsys) -> None:
    """反斜杠写法归一后走"快照未注册"报错，而非 volume_id 非法字符。"""
    rc = main(["hash", "NOPE\\20260101T000000Z", "--data-root", str(tmp_path)])
    out = capsys.readouterr()
    assert rc == 2
    assert "非法" not in out.err
    assert "NOPE/20260101T000000Z" in out.err  # 报错里已是归一后的 ID


def test_snapshot_id_backslash_diff_and_hash(tmp_path: Path, capsys) -> None:
    """diff 与 hash 入口：反斜杠与正斜杠两种写法等价。"""
    from test_diff import _mk_snapshot as mk
    from cold_manifest.catalog import (catalog_path, ensure_disk, ensure_volume,
                                       init_catalog, register_snapshot)
    a = mk(tmp_path, "VOL_P0", "20260101T000000Z",
           [("docs/a.txt", "file", 10, 111)])
    b = mk(tmp_path, "VOL_P0", "20260202T000000Z",
           [("docs/a.txt", "file", 20, 222)])
    # hash 需要真实源文件（host_path 指向的树）且快照在 catalog 注册
    src_a = tmp_path / "srcA"
    src_b = tmp_path / "srcB"
    (src_a / "docs").mkdir(parents=True)
    (src_b / "docs").mkdir(parents=True)
    (src_a / "docs" / "a.txt").write_bytes(b"x" * 10)
    (src_b / "docs" / "a.txt").write_bytes(b"y" * 20)
    cat = sqlite3.connect(catalog_path(tmp_path))
    init_catalog(cat)
    ensure_disk(cat, "DISK1", physical_model="TEST")
    ensure_volume(cat, "VOL_P0", "DISK1")
    register_snapshot(cat, a, "VOL_P0", status="sealed", host_path=str(src_a))
    register_snapshot(cat, b, "VOL_P0", status="sealed", host_path=str(src_b))
    cat.commit()
    cat.close()

    # hash：反斜杠写法成功
    rc = main(["hash", "VOL_P0\\20260101T000000Z", "--data-root", str(tmp_path)])
    assert rc == 0
    assert "哈希完成" in capsys.readouterr().out
    # hash：正斜杠写法同样成功（幂等复算）
    rc = main(["hash", a, "--data-root", str(tmp_path)])
    assert rc == 0
    capsys.readouterr()

    # diff：反斜杠写法与正斜杠写法产出同一 diff_id（options/快照完全一致）
    rc = main(["diff", "VOL_P0\\20260101T000000Z", "VOL_P0\\20260202T000000Z",
               "--data-root", str(tmp_path)])
    assert rc in (0, 1)  # 1=有差异（正常），2 才是错误
    out = capsys.readouterr().out
    assert "非法" not in out
    did_bs = out.split("diff 完成：")[1].split()[0]
    rc = main(["diff", a, b, "--data-root", str(tmp_path)])
    out2 = capsys.readouterr().out
    assert "复用既有结果库" in out2  # 斜杠写法命中反斜杠写法产出的同一结果库
    did_slash = out2.split("diff 完成：")[1].split("（")[0].strip()
    assert did_bs == did_slash


def test_snapshot_id_illegal_still_rejected(tmp_path: Path, capsys) -> None:
    """归一只针对分隔符：真正非法的 volume_id 字符仍然报错。"""
    rc = main(["hash", "BAD ID!/20260101T000000Z", "--data-root", str(tmp_path)])
    out = capsys.readouterr()
    assert rc == 2
    assert "非法" in out.err
