"""export 命令测试：csv 全字段导出 + v1_csv 三件套 + 回灌往返自检。"""

import csv
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from cold_manifest.cli import main as cli_main
from cold_manifest.import_legacy import import_snapshot

REPO = Path(__file__).resolve().parent.parent
# v1 源码与样本已在 2026-10-02 从仓库清理（用户决定全面改用 v2）。
# 如需重跑 v1 对照测试，把 CLDM_LEGACY_SAMPLES 指向任意含 backuptools/ 的 v1 目录即可。
LEGACY_SRC = REPO / os.environ.get("CLDM_LEGACY_SAMPLES", "Legacy/backupStatus260505")


def _make_legacy_dir(root: Path) -> Path:
    """合成 v1 快照目录（与 test_import 同构：3 文件 / 3 目录 / 2 warnings）。"""
    d = root / "legacy_snap"
    d.mkdir(parents=True)
    (d / "metadata.csv").write_text(
        "metric_name,value\n"
        "volume_id,TESTVOL_P0\n"
        "disk_serial,SER123\n"
        "collect_time_utc,2026-05-04T12:34:16Z\n"
        "collect_time_utc,2026-05-04T12:34:16Z\n"  # 重复键不应崩溃（meta 覆盖写）
        "filesystem,NTFS\n",
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
    with (d / "warnings.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stage", "path", "warning_type", "detail"])
        w.writerow(["scan", "E:\\$RECYCLE.BIN", "system_file_skipped", ""])
    return d


@pytest.fixture()
def imported(tmp_path: Path):
    snap_dir = _make_legacy_dir(tmp_path / "src")
    data_root = tmp_path / "data1"
    result = import_snapshot(snap_dir, data_root)
    return tmp_path, data_root, result.snapshot_id, result.stats


def test_export_csv_full_fields(tmp_path: Path, imported, capsys) -> None:
    tmp_path, data_root, sid, _ = imported
    out = tmp_path / "snap.csv"
    assert cli_main(["export", sid, "--format", "csv",
                     "--output", str(out), "--data-root", str(data_root)]) == 0
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    # 全字段：与 entries 表列一致
    src = sqlite3.connect(str(data_root / "TESTVOL_P0" / "20260504T123416Z" / "snapshot.db"))
    cols = [d[1] for d in src.execute("PRAGMA table_info(entries)")]
    src.close()
    assert list(rows[0].keys()) == cols
    assert len(rows) == 7  # root + 3 dir + 3 file


def test_export_v1_csv_roundtrip(tmp_path: Path, imported, capsys) -> None:
    """v1_csv 导出 → import-legacy 回灌：计数与 total_bytes 一致。"""
    tmp_path, data_root, sid, stats1 = imported
    out_dir = tmp_path / "v1out"
    assert cli_main(["export", sid, "--format", "v1_csv",
                     "--output", str(out_dir), "--data-root", str(data_root)]) == 0
    for name in ("metadata.csv", "tree.csv", "warnings.csv"):
        assert (out_dir / name).is_file()

    # tree.csv 表头含动态 level_N，且只含 file/empty_dir 行
    with (out_dir / "tree.csv").open(newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        header = next(r)
        types = {row[3] for row in r}
    assert header[:6] == ["volume_id", "relative_path", "entry_name", "entry_type",
                          "size_bytes", "modified_utc"]
    assert types == {"file", "empty_dir"}
    n_levels = len([h for h in header if h.startswith("level_")])
    assert n_levels == 3  # max depth = 3

    with (out_dir / "warnings.csv").open(newline="", encoding="utf-8") as f:
        assert next(csv.reader(f)) == ["stage", "path", "warning_type", "detail"]

    # 回灌到第二个 data root，对账
    data_root2 = tmp_path / "data2"
    result2 = import_snapshot(out_dir, data_root2)
    s2 = result2.stats
    assert s2["file_count"] == stats1["file_count"] == 3
    assert s2["dir_count"] == stats1["dir_count"] == 3
    assert s2["total_bytes"] == stats1["total_bytes"] == 300
    assert s2["total_alloc"] == stats1["total_alloc"]
    assert s2["zero_byte_count"] == stats1["zero_byte_count"]
    assert s2["skipped_count"] == stats1["skipped_count"] == 1
    assert s2["max_depth"] == stats1["max_depth"] == 3
    # 回灌产物再导出应与首次导出逐字节一致（导出确定性）
    out_dir2 = tmp_path / "v1out2"
    assert cli_main(["export", result2.snapshot_id, "--format", "v1_csv",
                     "--output", str(out_dir2), "--data-root", str(data_root2)]) == 0
    assert (out_dir / "tree.csv").read_bytes() == (out_dir2 / "tree.csv").read_bytes()


@pytest.mark.skipif(not LEGACY_SRC.is_dir(), reason="Legacy 源码不存在")
def test_legacy_compare_consumes_v1_csv(tmp_path: Path, imported) -> None:
    """导出的 v1 三件套能被 legacy `backuptools compare` 直接消费（自比 0 差异）。"""
    tmp_path, data_root, sid, _ = imported
    out_dir = tmp_path / "v1out"
    assert cli_main(["export", sid, "--format", "v1_csv",
                     "--output", str(out_dir), "--data-root", str(data_root)]) == 0

    sys.path.insert(0, str(LEGACY_SRC))
    try:
        from backuptools.comparator import compare
        diff_csv = tmp_path / "diff.csv"
        compare(str(out_dir), str(out_dir), 0, str(diff_csv))
        rows = list(csv.DictReader(diff_csv.open(encoding="utf-8")))
        assert rows == []  # 自比：无差异行
    finally:
        sys.path.pop(0)


def test_export_errors(tmp_path: Path, imported, capsys) -> None:
    tmp_path, data_root, sid, _ = imported
    # 快照不存在 → 退出码 2
    assert cli_main(["export", "NOPE/1", "--data-root", str(data_root)]) == 2
    # v1_csv 缺 --output → 2
    assert cli_main(["export", sid, "--format", "v1_csv",
                     "--data-root", str(data_root)]) == 2
    # 默认 --format csv 不给 --output → 用默认文件名成功
    import os
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert cli_main(["export", sid, "--data-root", str(data_root)]) == 0
    finally:
        os.chdir(old)


@pytest.mark.skipif(not LEGACY_SRC.is_dir(), reason="Legacy 源码不存在")
def test_legacy_compare_v2_export_vs_original_zero_diff(tmp_path: Path, imported) -> None:
    """M1 回归：v2 导出目录 vs 原始 v1 样本目录，legacy compare 必须 0 差异行。

    原样本含非空目录 docs（v1 不写行，靠推导）；若 v2 把非空目录导成
    empty_dir 行，会对每个非空目录报 type_diff（样本 B 曾是 5.1 万行噪声）。
    """
    tmp_path, data_root, sid, _ = imported
    out_dir = tmp_path / "v1out"
    assert cli_main(["export", sid, "--format", "v1_csv",
                     "--output", str(out_dir), "--data-root", str(data_root)]) == 0
    src_dir = tmp_path / "src" / "legacy_snap"

    sys.path.insert(0, str(LEGACY_SRC))
    try:
        from backuptools.comparator import compare
        diff_csv = tmp_path / "diff.csv"
        compare(str(src_dir), str(out_dir), 0, str(diff_csv))
        rows = list(csv.DictReader(diff_csv.open(encoding="utf-8")))
        assert rows == []  # 跨代：0 差异
    finally:
        sys.path.pop(0)
