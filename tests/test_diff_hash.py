"""③-3b 哈希联动 diff：content_changed / moved_or_renamed 识别与选项隔离。"""

import sqlite3
from pathlib import Path

import pytest

from cold_manifest.catalog import snapshot_path
from cold_manifest.diff_engine import (DiffError, canonical_options, compute_diff_id,
                                       compute_options_hash, materialize_diff)
from cold_manifest.schema import SNAPSHOT_DDL

H1 = "aa" * 32
H2 = "bb" * 32
H3 = "cc" * 32


def _mk_snapshot(tmp_path: Path, volume_id: str, ts: str,
                 rows: "list[tuple]") -> str:
    """建封库快照库。rows: (path, type, size, mtime[, hash_hex, hash_state])。"""
    sid = f"{volume_id}/{ts}"
    db = snapshot_path(tmp_path, sid)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(SNAPSHOT_DDL)
        conn.execute("INSERT INTO meta(key, value) VALUES('status', 'sealed')")
        data = [(1, 0, ".", "", 0, "dir", None, None, None, None, None)]
        paths_seen = {"."}
        eid = 2
        for row in rows:
            path, etype, size, mtime = row[:4]
            hash_hex = row[4] if len(row) > 4 else None
            hash_state = row[5] if len(row) > 5 else None
            parts = path.split("/")
            for i in range(1, len(parts)):
                parent = "/".join(parts[:i])
                if parent not in paths_seen:
                    data.append((eid, 1, parent, parts[i - 1], i, "dir",
                                 None, None, None))
                    paths_seen.add(parent)
                    eid += 1
            depth = path.count("/") + 1
            data.append((eid, 1, path, parts[-1], depth, etype, size, size, mtime,
                         hash_hex, hash_state))
            paths_seen.add(path)
            eid += 1
        conn.executemany(
            "INSERT INTO entries(entry_id,parent_id,path,name,depth,type,size_bytes,"
            "allocated_bytes,mtime_ns,hash_hex,hash_state)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)", data)
        conn.commit()
    finally:
        conn.close()
    return sid


@pytest.fixture()
def two_snaps(tmp_path: Path) -> "tuple[str, str]":
    """A/B：同路径同尺寸同 mtime 改写、改名不变内容、改名且改内容、普通 size/mtime。"""
    a = _mk_snapshot(tmp_path, "VOL_P0", "20260101T000000Z", [
        ("same_name.txt", "file", 10, 111, H1, "full"),      # B 改内容（尺寸/mtime 不变）
        ("stable.txt", "file", 10, 222, H3, "full"),         # 完全不变
        ("size_ch.txt", "file", 10, 333, H1, "full"),        # B 尺寸变
        ("mtime_ch.txt", "file", 10, 444, H3, "full"),       # B 仅 mtime 变
        ("orig_name.txt", "file", 20, 555, H2, "full"),      # B 改名（内容不变）
        ("gone_full.txt", "file", 30, 666, H1, "full"),      # B 改名且改内容
        ("gone_pure.txt", "file", 40, 777, H2, "full"),      # B 纯删除（无配对）
    ])
    b = _mk_snapshot(tmp_path, "VOL_P0", "20260202T000000Z", [
        ("same_name.txt", "file", 10, 111, H2, "full"),      # 同路径改内容
        ("stable.txt", "file", 10, 222, H3, "full"),
        ("size_ch.txt", "file", 99, 333, H2, "full"),
        ("mtime_ch.txt", "file", 10, 999999, H3, "full"),
        ("renamed.txt", "file", 20, 555, H2, "full"),        # 改名，内容不变
        ("new_full.txt", "file", 30, 666, H3, "full"),       # 改名且内容也变
    ])
    return a, b


def _types(db: Path) -> "dict[str, list]":
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        out: "dict[str, list]" = {}
        for ct, path, bp in conn.execute(
                "SELECT change_type, path, b_path FROM diff_entries"):
            out.setdefault(ct, []).append((path, bp))
        return out
    finally:
        conn.close()


def test_content_changed_and_moved(tmp_path: Path, two_snaps) -> None:
    a, b = two_snaps
    r = materialize_diff(tmp_path, a, b, options={"hash": "sha256"})
    t = _types(r.db_path)

    # 同名同尺寸同 mtime 但内容不同 → content_changed（优先于 size/mtime 分类）
    assert ("same_name.txt", None) in t["content_changed"]

    # 改名且内容不变 → moved_or_renamed，path 为 A 侧、b_path 为 B 侧
    assert ("orig_name.txt", "renamed.txt") in t["moved_or_renamed"]

    # 改名且内容也变 → removed + added，不造假 moved
    assert ("gone_full.txt", None) in t["removed"]
    assert ("new_full.txt", "new_full.txt") in t["added"]

    # 无配对的纯删除仍是 removed
    assert ("gone_pure.txt", None) in t["removed"]

    # 普通 mtime 变更不受影响；size 变更因两侧哈希可且不同 → content_changed
    # （哈希启用时 content_changed 优先于 size/mtime 分类）
    assert ("size_ch.txt", None) in t["content_changed"]
    assert ("mtime_ch.txt", None) in t["mtime_changed"]
    assert ("stable.txt", None) not in [
        p for lst in t.values() for p in lst]

    assert r.counts["content_changed"] == 2
    assert r.counts["moved_or_renamed"] == 1
    assert r.counts["removed"] == 2
    assert r.counts["added"] == 1

    # moved 行回填了 B 侧元数据与 a_hash/b_hash
    conn = sqlite3.connect(f"file:{r.db_path.as_posix()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT b_path, a_hash, b_hash, b_size FROM diff_entries"
            " WHERE change_type='moved_or_renamed'").fetchone()
        assert row == ("renamed.txt", H2, H2, 20)
    finally:
        conn.close()


def test_hash_none_keeps_null_counts(tmp_path: Path, two_snaps) -> None:
    a, b = two_snaps
    r = materialize_diff(tmp_path, a, b)
    assert r.counts["content_changed"] is None
    assert r.counts["moved_or_renamed"] is None
    t = _types(r.db_path)
    # 无哈希：改名各自归 removed/added
    assert ("orig_name.txt", None) in t["removed"]
    assert ("renamed.txt", "renamed.txt") in t["added"]
    # 不落 hash 列
    conn = sqlite3.connect(f"file:{r.db_path.as_posix()}?mode=ro", uri=True)
    try:
        ah, bh = conn.execute(
            "SELECT a_hash, b_hash FROM diff_entries LIMIT 1").fetchone()
        assert ah is None and bh is None
    finally:
        conn.close()


def test_hash_error_side_falls_back(tmp_path: Path) -> None:
    """任一侧 hash_state='error' → 该对不可用，回退 size/mtime 语义。"""
    a = _mk_snapshot(tmp_path, "VOL_P0", "20260101T000000Z", [
        ("f.txt", "file", 10, 111, "", "error"),
        ("g.txt", "file", 10, 111, H1, "full"),
    ])
    b = _mk_snapshot(tmp_path, "VOL_P0", "20260202T000000Z", [
        ("f.txt", "file", 10, 999999, H2, "full"),   # mtime 变但 A 侧无哈希
        ("g.txt", "file", 10, 111, H1, "full"),      # 哈希相同 → identical
    ])
    r = materialize_diff(tmp_path, a, b, options={"hash": "sha256"})
    assert r.counts["mtime_changed"] == 1
    assert r.counts["identical"] == 2   # root + g.txt（哈希相同）
    assert r.counts["content_changed"] == 0


def test_options_and_id_isolation(tmp_path: Path, two_snaps) -> None:
    a, b = two_snaps
    with pytest.raises(DiffError):
        canonical_options({"hash": "md5"})
    with pytest.raises(DiffError):
        canonical_options({"algo": "sha256"})
    assert canonical_options(None) == {"hash": "none"}
    assert canonical_options({"hash": "sha256"}) == {"hash": "sha256"}

    oh_none = compute_options_hash({"hash": "none"})
    oh_sha = compute_options_hash({"hash": "sha256"})
    assert oh_none != oh_sha
    assert compute_diff_id(a, b, oh_none) != compute_diff_id(a, b, oh_sha)

    # 各自幂等复用
    r1 = materialize_diff(tmp_path, a, b, options={"hash": "sha256"})
    r2 = materialize_diff(tmp_path, a, b, options={"hash": "sha256"})
    assert r2.reused and r2.diff_id == r1.diff_id and r2.counts == r1.counts
    r3 = materialize_diff(tmp_path, a, b)
    r4 = materialize_diff(tmp_path, a, b, options={"hash": "none"})
    assert r4.reused and r4.diff_id == r3.diff_id
    assert r3.diff_id != r1.diff_id


def test_api_and_cli_hash_option(tmp_path: Path, two_snaps, capsys) -> None:
    """API body hash 字段 + CLI --hash 走通；非法值 400 / 退出码 2。"""
    a, b = two_snaps
    from fastapi.testclient import TestClient

    from cold_manifest.server import create_app

    with TestClient(create_app(data_root=str(tmp_path))) as client:
        r = client.post("/api/diffs", json={"a": a, "b": b, "hash": "sha256"})
        assert r.status_code == 200
        assert r.json()["counts"]["moved_or_renamed"] == 1
        did = r.json()["diff_id"]
        r2 = client.post("/api/diffs", json={"a": a, "b": b, "hash": "sha256"})
        assert r2.json()["reused"] is True and r2.json()["diff_id"] == did
        # entries 暴露 hash 列
        items = client.get(f"/api/diffs/{did}/entries",
                           params={"category": "moved_or_renamed"}).json()["items"]
        assert items and items[0]["b_path"] == "renamed.txt"
        assert items[0]["a_hash"] == H2
        assert client.post("/api/diffs",
                           json={"a": a, "b": b, "hash": "md5"}).status_code == 400

    from cold_manifest.cli import main as cli_main
    assert cli_main(["diff", a, b, "--data-root", str(tmp_path),
                     "--hash", "sha256"]) == 1
    out = capsys.readouterr().out
    assert "moved_or_renamed" in out
    with pytest.raises(SystemExit):
        cli_main(["diff", a, b, "--data-root", str(tmp_path), "--hash", "bogus"])


def test_old_diff_db_readable(tmp_path: Path, two_snaps) -> None:
    """旧 diff 库（无 a_hash/b_hash/b_path 列）API 查询兼容。"""
    a, b = two_snaps
    r = materialize_diff(tmp_path, a, b)   # hash=none 但新代码仍写 hash 列（NULL）
    conn = sqlite3.connect(str(r.db_path))
    conn.execute("ALTER TABLE diff_entries DROP COLUMN a_hash")
    conn.execute("ALTER TABLE diff_entries DROP COLUMN b_hash")
    conn.execute("ALTER TABLE diff_entries DROP COLUMN b_path")
    conn.commit()
    conn.close()

    from fastapi.testclient import TestClient

    from cold_manifest.server import create_app

    with TestClient(create_app(data_root=str(tmp_path))) as client:
        body = client.get(f"/api/diffs/{r.diff_id}/entries").json()
        assert body["items"]
        assert all("a_hash" in it for it in body["items"])
