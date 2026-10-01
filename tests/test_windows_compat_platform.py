"""Windows 体验收尾（后端三项）：采集记平台标记 / diff 大小写提示 / 长路径预警。"""

import sqlite3
import sys
from pathlib import Path

import pytest

from cold_manifest.catalog import snapshot_path
from cold_manifest.collect import collect_volume
from cold_manifest.diff_engine import materialize_diff
from cold_manifest.probe import DiskInfo, VolumeInfo
from cold_manifest.schema import SNAPSHOT_DDL

from test_collect import _fake_probe


# ---------------------------------------------------------------- 快照夹具


def _mk_snapshot(tmp_path: Path, volume_id: str, ts: str,
                 rows: "list[tuple]", os_platform: "str | None") -> str:
    """封库快照：rows=(path, type, size, mtime)；os_platform=None 模拟旧快照。"""
    sid = f"{volume_id}/{ts}"
    db = snapshot_path(tmp_path, sid)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(SNAPSHOT_DDL)
        conn.execute("INSERT INTO meta(key, value) VALUES('status', 'sealed')")
        if os_platform is not None:
            conn.execute("INSERT INTO meta(key, value) VALUES('os_platform', ?)",
                         (os_platform,))
        data = [(1, 0, ".", "", 0, "dir", None, None, None)]
        eid = 2
        paths_seen = {"."}
        for path, etype, size, mtime in rows:
            parts = path.split("/")
            for i in range(1, len(parts)):
                parent = "/".join(parts[:i])
                if parent not in paths_seen:
                    data.append((eid, 1, parent, parts[i - 1], i, "dir",
                                 None, None, None))
                    paths_seen.add(parent)
                    eid += 1
            depth = path.count("/") + 1
            data.append((eid, 1, path, parts[-1], depth, etype, size, size, mtime))
            paths_seen.add(path)
            eid += 1
        conn.executemany(
            "INSERT INTO entries(entry_id,parent_id,path,name,depth,type,size_bytes,"
            "allocated_bytes,mtime_ns) VALUES(?,?,?,?,?,?,?,?,?)", data)
        conn.execute("UPDATE entries SET path_norm = lower(path)")
        conn.commit()
    finally:
        conn.close()
    return sid


# ---------------------------------------------------------------- ① 平台标记


def test_collect_writes_os_platform(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("cold_manifest.collect.probe_path", _fake_probe())
    scan_root = tmp_path / "vol"
    (scan_root / "d").mkdir(parents=True)
    (scan_root / "d" / "a.txt").write_bytes(b"x")
    result = collect_volume(scan_root, data_root=tmp_path / "data",
                            on_disk_copy=False, smartctl=False)
    conn = sqlite3.connect(str(result.db_path))
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='os_platform'").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == sys.platform


# ---------------------------------------------------------------- ② 大小写提示


@pytest.fixture()
def win_pair(tmp_path: Path) -> "tuple[str, str]":
    """两个 Windows 快照：a.txt 改名为 A.TXT（精确 path 比对 = 1 删 + 1 增）。"""
    a = _mk_snapshot(tmp_path, "WIN_P0", "20260101T000000Z",
                     [("a.txt", "file", 5, 111)], os_platform="win32")
    b = _mk_snapshot(tmp_path, "WIN_P0", "20260202T000000Z",
                     [("A.TXT", "file", 5, 222)], os_platform="win32")
    return a, b


def test_hint_case_insensitive_on_win32(tmp_path: Path, win_pair) -> None:
    a, b = win_pair
    result = materialize_diff(tmp_path, a, b, options={})
    assert result.hints and "case_insensitive" in result.hints[0]
    # 落 diff_meta
    import json
    row = sqlite3.connect(str(result.db_path)).execute(
        "SELECT value FROM diff_meta WHERE key='hints_json'").fetchone()
    assert row is not None
    assert "case_insensitive" in json.loads(row[0])[0]
    # 复用路径同样带回
    again = materialize_diff(tmp_path, a, b, options={})
    assert again.reused and again.hints == result.hints


def test_hint_suppressed_with_case_insensitive(tmp_path: Path, win_pair) -> None:
    a, b = win_pair
    result = materialize_diff(tmp_path, a, b, options={"case_insensitive": True})
    assert result.hints == []
    row = sqlite3.connect(str(result.db_path)).execute(
        "SELECT value FROM diff_meta WHERE key='hints_json'").fetchone()
    import json
    assert json.loads(row[0]) == []
    # 且改名被正确配对：无 removed/added
    assert result.counts["removed"] == 0 and result.counts["added"] == 0


def test_hint_absent_without_platform_mark(tmp_path: Path) -> None:
    a = _mk_snapshot(tmp_path, "OLD_P0", "20260101T000000Z",
                     [("a.txt", "file", 5, 111)], os_platform=None)
    b = _mk_snapshot(tmp_path, "OLD_P0", "20260202T000000Z",
                     [("A.TXT", "file", 5, 222)], os_platform=None)
    result = materialize_diff(tmp_path, a, b, options={})
    assert result.hints == []


# ---------------------------------------------------------------- ③ 长路径预警


def test_long_path_warning_win32(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("cold_manifest.collect.probe_path", _fake_probe())
    # 构造 >200 字符的扫描根
    deep = tmp_path
    while len(str(deep)) <= 210:
        deep = deep / ("segment-" + "x" * 20)
    deep.mkdir(parents=True, exist_ok=True)
    (deep / "f.txt").write_bytes(b"x")
    assert len(str(deep)) > 200
    result = collect_volume(deep, data_root=tmp_path / "data",
                            on_disk_copy=False, smartctl=False)
    assert any(w.startswith("long_path=") for w in result.warnings)
    conn = sqlite3.connect(str(result.db_path))
    try:
        row = conn.execute(
            "SELECT warning_type FROM skipped WHERE warning_type='long_path'").fetchone()
    finally:
        conn.close()
    assert row is not None


def test_no_long_path_warning_on_linux_short_root(tmp_path: Path,
                                                  probe_env_light) -> None:
    scan_root = tmp_path / "vol"
    (scan_root / "d").mkdir(parents=True)
    (scan_root / "d" / "a.txt").write_bytes(b"x")
    result = collect_volume(scan_root, data_root=tmp_path / "data",
                            on_disk_copy=False, smartctl=False)
    assert not any(w.startswith("long_path=") for w in result.warnings)
    conn = sqlite3.connect(str(result.db_path))
    try:
        row = conn.execute(
            "SELECT 1 FROM skipped WHERE warning_type='long_path'").fetchone()
    finally:
        conn.close()
    assert row is None


@pytest.fixture()
def probe_env_light(monkeypatch):
    monkeypatch.setattr("cold_manifest.collect.probe_path", _fake_probe())
