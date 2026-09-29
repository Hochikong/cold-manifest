"""v1 快照导入测试：小合成样本 + 真样本（样本 B 小盘 / 大盘 skipif）。"""

import csv
import os
import sqlite3
from pathlib import Path

import pytest

from cold_manifest.catalog import find_snapshot, snapshot_path
from cold_manifest.import_legacy import LegacyImportError, import_snapshot
from cold_manifest.schema import SNAPSHOT_DDL


def _make_legacy_dir(root: Path) -> Path:
    """合成快照目录：含逗号/引号文件名、嵌套目录、empty_dir 行、warnings 重复行。"""
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
                    "size_bytes", "modified_utc", "level_1", "level_2", "level_3"])
        w.writerow(["TESTVOL_P0", "docs/a.txt", "a.txt", "file", "100", "2026-01-02T03:04:05Z", "docs", "a.txt"])
        w.writerow(["TESTVOL_P0", 'docs/weird, "q".txt', 'weird, "q".txt', "file",
                    "200", "2026-02-03T04:05:06Z", "docs", 'weird, "q".txt'])
        w.writerow(["TESTVOL_P0", "docs/sub/b.bin", "b.bin", "file", "0", "2026-03-04T05:06:07Z",
                    "docs", "sub", "b.bin"])
        w.writerow(["TESTVOL_P0", "emptydir", "emptydir", "empty_dir", "", "2026-04-05T06:07:08Z", "emptydir"])
        # 兼容：显式 "dir" token 命中已推导路径（docs），应回填 mtime 而非当作文件/重复
        w.writerow(["TESTVOL_P0", "docs", "docs", "dir", "", "2026-05-06T07:08:09Z", "docs"])
    with (d / "warnings.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stage", "path", "warning_type", "detail"])
        w.writerow(["scan", "E:\\$RECYCLE.BIN", "system_file_skipped", ""])
        w.writerow(["scan", "E:\\$RECYCLE.BIN", "system_file_skipped", ""])
        w.writerow(["scan", "E:\\System Volume Information", "system_file_skipped", "x"])
    return d


def _open(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def test_import_synthetic(tmp_path: Path) -> None:
    snap = _make_legacy_dir(tmp_path)
    data_root = tmp_path / "dataroot"
    result = import_snapshot(snap, data_root)

    assert result.snapshot_id == "TESTVOL_P0/20260504T123416Z"
    assert not result.skipped_import
    s = result.stats
    assert s["file_count"] == 3
    # docs、docs/sub、emptydir = 3 个目录（不含 root）
    assert s["dir_count"] == 3
    assert s["total_bytes"] == 300
    assert s["total_alloc"] == 300
    assert s["zero_byte_count"] == 1
    assert s["max_depth"] == 3
    assert s["skipped_count"] == 2  # warnings 重复行去重

    db = snapshot_path(data_root, result.snapshot_id)
    assert db.is_file()
    conn = _open(db)

    # root 行
    root = conn.execute("SELECT * FROM entries WHERE entry_id=1").fetchone()
    assert (root["parent_id"], root["path"], root["name"], root["depth"], root["type"]) == (0, ".", "", 0, "dir")

    # 抽查文件：ext / path_norm / mtime_ns / 父子关系
    row = conn.execute("SELECT * FROM entries WHERE name='a.txt'").fetchone()
    assert row["ext"] == ".txt"
    assert row["path_norm"] == "docs/a.txt"
    import calendar
    import time as _t

    # 2026-01-02T03:04:05Z（用 calendar.timegm 独立换算对账）
    assert row["mtime_ns"] == calendar.timegm(
        _t.strptime("2026-01-02T03:04:05Z", "%Y-%m-%dT%H:%M:%SZ")) * 1_000_000_000
    parent = conn.execute("SELECT path FROM entries WHERE entry_id=?", (row["parent_id"],)).fetchone()
    assert parent["path"] == "docs"

    weird = conn.execute("SELECT * FROM entries WHERE path LIKE 'docs/weird%'").fetchone()
    assert weird["size_bytes"] == 200
    assert weird["ext"] == ".txt"

    # 无点文件名 ext=''（合成样本没有；补一个直接验证 _ext_of）
    from cold_manifest.import_legacy import _ext_of
    assert _ext_of("README") == ""
    assert _ext_of("archive.tar.gz") == ".gz"
    assert _ext_of(".hidden") == ""

    # empty_dir：size NULL，且显式行的 mtime 保留
    ed = conn.execute("SELECT * FROM entries WHERE path='emptydir'").fetchone()
    assert ed["type"] == "dir" and ed["size_bytes"] is None
    assert ed["mtime_ns"] == calendar.timegm(
        _t.strptime("2026-04-05T06:07:08Z", "%Y-%m-%dT%H:%M:%SZ")) * 1_000_000_000

    # 显式 "dir" 行命中已推导路径：mtime 回填成功
    docs_row = conn.execute("SELECT * FROM entries WHERE path='docs'").fetchone()
    assert docs_row["type"] == "dir"
    assert docs_row["mtime_ns"] == calendar.timegm(
        _t.strptime("2026-05-06T07:08:09Z", "%Y-%m-%dT%H:%M:%SZ")) * 1_000_000_000

    # meta 键
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["imported_from"] == "legacy_v1_csv"
    assert meta["dirs_source"] == "derived"
    assert meta["allocated_source"] == "logical_size_fallback"
    assert meta["status"] == "sealed"
    assert meta["volume_id"] == "TESTVOL_P0"
    assert "import_time_utc" in meta
    assert "metric_name" not in meta  # B5：表头行不写入 meta

    # skipped 去重与路径规整
    sk = conn.execute("SELECT path FROM skipped ORDER BY path").fetchall()
    assert [r["path"] for r in sk] == ["$RECYCLE.BIN", "System Volume Information"]

    # dir_rollup 手工验算（后代统计，不含自身）
    ru = {r["entry_id"]: r for r in conn.execute("SELECT * FROM dir_rollup").fetchall()}
    docs = conn.execute("SELECT entry_id FROM entries WHERE path='docs'").fetchone()["entry_id"]
    assert ru[docs]["file_count"] == 3
    assert ru[docs]["total_bytes"] == 300
    assert ru[docs]["dir_count"] == 1  # sub
    sub = conn.execute("SELECT entry_id FROM entries WHERE path='docs/sub'").fetchone()["entry_id"]
    assert ru[sub]["file_count"] == 1 and ru[sub]["total_bytes"] == 0
    ed_id = ed["entry_id"]
    assert ru[ed_id]["file_count"] == 0
    root_ru = ru[1]
    assert root_ru["file_count"] == 3 and root_ru["total_bytes"] == 300

    # catalog 注册
    from cold_manifest.catalog import connect_catalog
    cat = connect_catalog(data_root)
    snap_row = find_snapshot(cat, result.snapshot_id)
    assert snap_row is not None
    assert snap_row["status"] == "sealed" and snap_row["hash_policy"] == "none"
    assert snap_row["file_count"] == 3 and snap_row["skipped_count"] == 2
    vol = cat.execute("SELECT * FROM volumes WHERE volume_id='TESTVOL_P0'").fetchone()
    assert vol["disk_id"] == "SER123" and vol["filesystem"] == "NTFS"
    assert cat.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1

    # 幂等：再次导入应跳过
    again = import_snapshot(snap, data_root)
    assert again.skipped_import
    # force：删除重建
    forced = import_snapshot(snap, data_root, force=True)
    assert not forced.skipped_import
    conn.close()


def test_import_missing_metadata(tmp_path: Path) -> None:
    with pytest.raises(LegacyImportError):
        import_snapshot(tmp_path, tmp_path / "data")


def test_reimport_dirty_db(tmp_path: Path) -> None:
    """目标 snapshot.db 存在但未封库（崩溃残留）→ 无须 force 即删除重建。"""
    snap = _make_legacy_dir(tmp_path)
    data_root = tmp_path / "dataroot"
    first = import_snapshot(snap, data_root)
    assert not first.skipped_import
    db = snapshot_path(data_root, first.snapshot_id)

    # 造"半截库"：只有表、没有 sealed 标记
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM meta WHERE key='status'")
    conn.commit()
    conn.close()

    again = import_snapshot(snap, data_root)  # 不带 force
    assert not again.skipped_import
    conn = _open(db)
    assert dict(conn.execute("SELECT key, value FROM meta WHERE key='status'").fetchone())["value"] == "sealed"
    conn.close()


def test_reject_invalid_volume_id(tmp_path: Path) -> None:
    """volume_id 含 / \\ .. 等非法字符时拒绝导入。"""
    snap = _make_legacy_dir(tmp_path)
    for bad in ["../evil", "a/b", "a\\b", "..", ".hidden"]:
        with pytest.raises(LegacyImportError):
            import_snapshot(snap, tmp_path / "d", volume_id=bad)


SMALL = Path("Legacy/backupStatus260505/TOSHIBA_HDWD120_X0DG6A2GS_P0")
BIG = Path("Legacy/backupStatus260505/TOSHIBA_HDWD120_X0FGBM2AS_P0")


@pytest.mark.skipif(not SMALL.is_dir(), reason="样本 A 目录不存在")
def test_import_real_small(tmp_path: Path) -> None:
    result = import_snapshot(SMALL, tmp_path)
    assert result.stats["file_count"] == 22
    assert result.stats["skipped_count"] == 2

    # 独立遍历 tree.csv 推导父目录集合（排除 root）
    parents: set = set()
    with (SMALL / "tree.csv").open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rel = row["relative_path"]
            parents.add(rel.rsplit("/", 1)[0] if "/" in rel else "")
    parents.discard("")
    conn = _open(snapshot_path(tmp_path, result.snapshot_id))
    db_dirs = {r["path"] for r in conn.execute(
        "SELECT path FROM entries WHERE type='dir' AND entry_id != 1")}
    assert db_dirs == parents

    row = conn.execute("SELECT * FROM entries WHERE name='20260213.rar'").fetchone()
    assert row["size_bytes"] == 507252615
    import calendar
    import time as _t

    assert row["mtime_ns"] == calendar.timegm(
        _t.strptime("2026-04-30T05:32:13Z", "%Y-%m-%dT%H:%M:%SZ")) * 1_000_000_000
    conn.close()


@pytest.mark.skipif(not BIG.is_dir() or os.environ.get("SKIP_BIG_IMPORT"), reason="样本 B 不存在或被禁用")
def test_import_real_big(tmp_path: Path) -> None:
    result = import_snapshot(BIG, tmp_path)
    s = result.stats
    assert s["file_count"] == 2_981_921
    assert s["max_depth"] == 8
    assert s["skipped_count"] == 3_252
    assert abs(s["total_bytes"] - 1_874_400_000_000) < 1_000_000_000  # ≈1874.4 GB（十进制）
    conn = _open(snapshot_path(tmp_path, result.snapshot_id))
    n_dirs = conn.execute("SELECT COUNT(*) FROM entries WHERE type='dir' AND entry_id != 1").fetchone()[0]
    n_dirs_root = conn.execute("SELECT COUNT(*) FROM entries WHERE type='dir'").fetchone()[0]
    print(f"BIG: dirs={n_dirs} (incl root {n_dirs_root})")
    rollup_root = conn.execute(
        "SELECT file_count, total_bytes FROM dir_rollup WHERE entry_id=1").fetchone()
    assert rollup_root["file_count"] == s["file_count"]
    assert rollup_root["total_bytes"] == s["total_bytes"]
    conn.close()
