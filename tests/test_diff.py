"""diff 引擎与 cldm diff CLI 测试：tmp 内合成两个小快照库。"""

import sqlite3
from pathlib import Path

import pytest

from cold_manifest.catalog import snapshot_path
from cold_manifest.cli import main as cli_main
from cold_manifest.db import init_snapshot
from cold_manifest.diff_engine import (DiffError, compute_diff_id, compute_options_hash,
                                       materialize_diff)
from cold_manifest.schema import SNAPSHOT_DDL

# (path, type, size, mtime) —— root '.' 两库一致，不写也会被 join 判 identical？root 需存在：
# 导入引擎总有 root 行；此处手工建库也带 root。


def _mk_snapshot(tmp_path: Path, volume_id: str, ts: str,
                 rows: "list[tuple]") -> str:
    """建一个封库快照库，返回 snapshot_id。rows: (path, type, size, mtime)。"""
    sid = f"{volume_id}/{ts}"
    db = snapshot_path(tmp_path, sid)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(SNAPSHOT_DDL)
        conn.execute("INSERT INTO meta(key, value) VALUES('status', 'sealed')")
        data = [(1, 0, ".", "", 0, "dir", None, None, None)]
        paths_seen = {"."}
        eid = 2
        for path, etype, size, mtime in rows:
            # 自动补父目录链（与导入引擎的 dirs_source=derived 一致）
            parts = path.split("/")
            for i in range(1, len(parts)):
                parent = "/".join(parts[:i])
                if parent not in paths_seen:
                    data.append((eid, 1, parent, parts[i - 1], i, "dir", None, None, None))
                    paths_seen.add(parent)
                    eid += 1
            depth = path.count("/") + 1
            data.append((eid, 1, path, parts[-1], depth, etype, size, size, mtime))
            paths_seen.add(path)
            eid += 1
        conn.executemany(
            "INSERT INTO entries(entry_id,parent_id,path,name,depth,type,size_bytes,"
            "allocated_bytes,mtime_ns) VALUES(?,?,?,?,?,?,?,?,?)", data)
        conn.commit()
    finally:
        conn.close()
    return sid


@pytest.fixture()
def two_snaps(tmp_path: Path) -> "tuple[str, str]":
    a = _mk_snapshot(tmp_path, "VOL_P0", "20260101T000000Z", [
        ("docs", "dir", None, 100),
        ("docs/same.txt", "file", 10, 111),
        ("docs/size.txt", "file", 10, 222),
        ("docs/mtime.txt", "file", 10, 333),
        ("docs/typeme", "file", 5, 444),
        ("gone/only_a.txt", "file", 7, 555),
    ])
    b = _mk_snapshot(tmp_path, "VOL_P0", "20260202T000000Z", [
        ("docs", "dir", None, 100),
        ("docs/same.txt", "file", 10, 111),
        ("docs/size.txt", "file", 99, 222),
        ("docs/mtime.txt", "file", 10, 999999),
        ("docs/typeme", "dir", None, 444),
        ("new/only_b.txt", "file", 3, 666),
    ])
    return a, b


def test_engine_categories(tmp_path: Path, two_snaps) -> None:
    a, b = two_snaps
    r = materialize_diff(tmp_path, a, b)
    assert not r.reused
    assert r.counts["added"] == 2  # new 目录 + new/only_b.txt
    assert r.counts["removed"] == 2  # gone 目录 + gone/only_a.txt
    assert r.counts["size_changed"] == 1
    assert r.counts["mtime_changed"] == 1
    assert r.counts["type_changed"] == 1
    # identical：'.' + 'docs' + 'docs/same.txt'（'gone'/'new' 目录各归 removed/added）
    assert r.counts["identical"] == 3
    assert r.counts["content_changed"] is None      # 无哈希不造假
    assert r.counts["moved_or_renamed"] is None
    assert r.db_path.is_file()
    assert r.db_path == tmp_path / "_diffs" / f"{r.diff_id}.db"
    assert len(r.diff_id) == 16

    # 落库行不含 identical
    conn = sqlite3.connect(str(r.db_path))
    types = dict(conn.execute("SELECT change_type, COUNT(*) FROM diff_entries GROUP BY 1"))
    conn.close()
    assert "identical" not in types
    assert types["added"] == 2 and types["removed"] == 2 and types["type_changed"] == 1


def test_idempotent_reuse(tmp_path: Path, two_snaps) -> None:
    a, b = two_snaps
    r1 = materialize_diff(tmp_path, a, b)
    mtime1 = r1.db_path.stat().st_mtime_ns
    r2 = materialize_diff(tmp_path, a, b)
    assert r2.reused and r2.diff_id == r1.diff_id
    assert r2.db_path.stat().st_mtime_ns == mtime1
    assert r2.counts == r1.counts

    # 未知选项 / 非法 hash 值 → DiffError（不污染幂等键）
    with pytest.raises(DiffError):
        materialize_diff(tmp_path, a, b, options={"depth": 0})
    with pytest.raises(DiffError):
        materialize_diff(tmp_path, a, b, options={"hash": "md5"})
    assert compute_options_hash(None) == compute_options_hash({"hash": "none"})
    # sha256 是合法选项，与 none 产生不同 options_hash / diff_id
    assert compute_options_hash({"hash": "sha256"}) != compute_options_hash({"hash": "none"})


def test_force_rebuild(tmp_path: Path, two_snaps) -> None:
    a, b = two_snaps
    r1 = materialize_diff(tmp_path, a, b)
    r2 = materialize_diff(tmp_path, a, b, force=True)
    assert not r2.reused and r2.diff_id == r1.diff_id
    assert r2.counts == r1.counts


def test_missing_snapshot_raises(tmp_path: Path, two_snaps) -> None:
    a, _ = two_snaps
    with pytest.raises(DiffError):
        materialize_diff(tmp_path, a, "NOPE_P0/20990101T000000Z")


def test_diff_id_formula() -> None:
    import hashlib
    ohash = compute_options_hash(None)
    expect = hashlib.sha1(f"A|B|{ohash}".encode()).hexdigest()[:16]
    assert compute_diff_id("A", "B", ohash) == expect


def _run_cli(capsys, *argv: str) -> int:
    capsys.readouterr()
    return cli_main(list(argv))


def test_cli_diff_exit_codes(tmp_path: Path, two_snaps, capsys) -> None:
    a, b = two_snaps
    # 无差异：自身对自身 → 0
    assert _run_cli(capsys, "diff", a, a, "--data-root", str(tmp_path)) == 0
    # 有差异 → 1
    assert _run_cli(capsys, "diff", a, b, "--data-root", str(tmp_path)) == 1
    out = capsys.readouterr().out
    assert "added" in out and "identical" in out

    # --output CSV
    csv_path = tmp_path / "d.csv"
    assert _run_cli(capsys, "diff", a, b, "--data-root", str(tmp_path),
                    "--output", str(csv_path)) == 1
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "category,path,type,size_a,size_b,mtime_a,mtime_b"
    by_cat = {ln.split(",")[0] for ln in lines[1:]}
    assert by_cat == {"added", "removed", "size_changed", "mtime_changed", "type_changed"}

    # 错误：快照不存在 → 2
    assert _run_cli(capsys, "diff", a, "NOPE/1", "--data-root", str(tmp_path)) == 2


def test_cli_diff_depth_filter(tmp_path: Path, two_snaps, capsys) -> None:
    a, b = two_snaps
    csv_path = tmp_path / "d1.csv"
    assert _run_cli(capsys, "diff", a, b, "--data-root", str(tmp_path),
                    "--output", str(csv_path), "--depth", "1") == 1
    body = csv_path.read_text(encoding="utf-8").splitlines()[1:]
    # depth<=1 只剩 docs 下深度 1 的条目与顶层目录（本例 changed 条目都在深度 2，只剩无）
    # type_changed 行 docs/typeme 深度 2 被过滤
    assert all(p.split(",")[1].count("/") <= 0 for p in body)
