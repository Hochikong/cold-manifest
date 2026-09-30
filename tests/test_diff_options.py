"""diff 口径开关（§6.3）：case_insensitive / ignore_mtime / ignore_size /
show_identical + evidence / evidence_level。

夹具为生产布局（path_norm = casefold 全路径），同时覆盖 SQL 快路径与
case_insensitive 的 Python 归并路径；默认参数回归比对参照 tests/test_diff.py
的无 path_norm 夹具（Python 回退路径）。
"""

import json
import sqlite3
from pathlib import Path

import pytest

from cold_manifest.catalog import snapshot_path
from cold_manifest.cli import main as cli_main
from cold_manifest.db import init_snapshot
from cold_manifest.diff_engine import (DiffError, canonical_options,
                                       compute_options_hash, materialize_diff,
                                       _read_evidence)
from cold_manifest.schema import SNAPSHOT_DDL


def _mk_snapshot(tmp_path: Path, volume_id: str, ts: str,
                 rows: "list[tuple]", with_path_norm: bool = True,
                 hashes: "dict | None" = None) -> str:
    """建一个封库快照库。rows: (path, type, size, mtime)。
    with_path_norm=False 时留空 path_norm（模拟旧采集）。
    hashes: {path: (hash_hex, hash_state)} 可选写哈希列。
    """
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
        if with_path_norm:
            conn.execute("UPDATE entries SET path_norm = lower(path)")
        if hashes:
            for path, (hx, st) in hashes.items():
                conn.execute("UPDATE entries SET hash_hex=?, hash_state=?"
                             " WHERE path=?", (hx, st, path))
        conn.commit()
    finally:
        conn.close()
    return sid


@pytest.fixture()
def base_snaps(tmp_path: Path) -> "tuple[str, str]":
    """有 path_norm 的基础夹具：size/mtime/类型差异 + removed/added + identical。"""
    a = _mk_snapshot(tmp_path, "VOL_P0", "20260101T000000Z", [
        ("docs/same.txt", "file", 10, 111),
        ("docs/size.txt", "file", 10, 222),
        ("docs/mtime.txt", "file", 10, 333),
        ("docs/typeme", "file", 5, 444),
        ("gone/only_a.txt", "file", 7, 555),
    ])
    b = _mk_snapshot(tmp_path, "VOL_P0", "20260202T000000Z", [
        ("docs/same.txt", "file", 10, 111),
        ("docs/size.txt", "file", 99, 222),
        ("docs/mtime.txt", "file", 10, 999999),
        ("docs/typeme", "dir", None, 444),
        ("new/only_b.txt", "file", 3, 666),
    ])
    return a, b


# ---------------------------------------------------------------- 白名单


def test_canonical_options_whitelist() -> None:
    assert canonical_options(None) == {"hash": "none"}
    assert canonical_options({"hash": "sha256", "ignore_mtime": True}) == \
        {"hash": "sha256", "ignore_mtime": True}
    # false 布尔值从 canonical 形式剔除（与缺省同键，不污染幂等键）
    assert canonical_options({"ignore_size": False}) == {"hash": "none"}
    with pytest.raises(DiffError):
        canonical_options({"depth": 2})
    with pytest.raises(DiffError):
        canonical_options({"case_insensitive": "yes"})
    with pytest.raises(DiffError):
        canonical_options({"hash": "md5"})


def test_options_hash_isolation(base_snaps) -> None:
    variants = [
        {},
        {"hash": "sha256"},
        {"case_insensitive": True},
        {"ignore_mtime": True},
        {"ignore_size": True},
        {"show_identical": True},
        {"hash": "sha256", "ignore_mtime": True},
    ]
    hashes = [compute_options_hash(v) for v in variants]
    assert len(set(hashes)) == len(hashes)          # 各组合天然隔离
    assert compute_options_hash(None) == compute_options_hash({})


# ---------------------------------------------------------------- 开关语义


def test_ignore_mtime_semantics(tmp_path: Path, base_snaps) -> None:
    a, b = base_snaps
    r_default = materialize_diff(tmp_path, a, b)
    assert r_default.counts["mtime_changed"] == 1
    r = materialize_diff(tmp_path, a, b, options={"ignore_mtime": True})
    assert r.counts["mtime_changed"] == 0
    # docs/mtime.txt 只有 mtime 差异 → 归 identical
    assert r.counts["identical"] == r_default.counts["identical"] + 1


def test_ignore_size_semantics(tmp_path: Path, base_snaps) -> None:
    a, b = base_snaps
    r_default = materialize_diff(tmp_path, a, b)
    assert r_default.counts["size_changed"] == 1
    r = materialize_diff(tmp_path, a, b, options={"ignore_size": True})
    assert r.counts["size_changed"] == 0
    assert r.counts["identical"] == r_default.counts["identical"] + 1
    # 类型差异不受影响
    assert r.counts["type_changed"] == 1


def test_show_identical_stores_rows(tmp_path: Path, base_snaps) -> None:
    a, b = base_snaps
    r_default = materialize_diff(tmp_path, a, b)
    conn = sqlite3.connect(str(r_default.db_path))
    n_default = conn.execute("SELECT COUNT(*) FROM diff_entries").fetchone()[0]
    conn.close()

    r = materialize_diff(tmp_path, a, b, options={"show_identical": True})
    assert r.counts["identical"] == r_default.counts["identical"]
    conn = sqlite3.connect(str(r.db_path))
    types = dict(conn.execute(
        "SELECT change_type, COUNT(*) FROM diff_entries GROUP BY 1"))
    conn.close()
    assert types.get("identical") == r.counts["identical"]
    assert sum(types.values()) == n_default + r.counts["identical"]
    # 两个 diff_id 不同（options_hash 隔离），互不影响
    assert r.diff_id != r_default.diff_id


# ---------------------------------------------------------------- case_insensitive


def test_case_insensitive_pairs_and_moved(tmp_path: Path) -> None:
    a = _mk_snapshot(tmp_path, "VOL_CI", "20260101T000000Z", [
        ("docs/ReadMe.TXT", "file", 10, 111),
        ("gone/OLD.txt", "file", 7, 222),
    ], hashes={"gone/OLD.txt": ("deadbeef" * 8, "full")})
    b = _mk_snapshot(tmp_path, "VOL_CI", "20260202T000000Z", [
        ("docs/readme.txt", "file", 10, 999),      # 大小写不同 → 配对
        ("new/renamed.md", "file", 7, 333),        # 改名（哈希同）→ moved
    ], hashes={
        "gone/OLD.txt": ("deadbeef" * 8, "full"),
        "new/renamed.md": ("deadbeef" * 8, "full"),
    })
    r = materialize_diff(tmp_path, a, b,
                         options={"case_insensitive": True, "hash": "sha256"})
    assert r.counts["identical"] == 2        # root + docs 目录（折叠后同名）
    # docs/ReadMe.TXT ↔ docs/readme.txt 大小写配对，mtime 差异 → mtime_changed
    assert r.counts["mtime_changed"] == 1
    # gone/OLD.txt → new/renamed.md：合并为 moved；gone/ 与 new/ 两个无哈希
    # 目录仍各归 removed/added
    assert r.counts["removed"] == 1 and r.counts["added"] == 1
    assert r.counts["moved_or_renamed"] == 1

    conn = sqlite3.connect(str(r.db_path))
    rows = dict((ct, path) for ct, path in conn.execute(
        "SELECT change_type, path FROM diff_entries"))
    b_path = dict(conn.execute(
        "SELECT change_type, b_path FROM diff_entries WHERE b_path IS NOT NULL"))
    conn.close()
    assert rows.get("mtime_changed") == "docs/ReadMe.TXT"
    assert rows.get("moved_or_renamed") == "gone/OLD.txt"
    assert b_path.get("moved_or_renamed") == "new/renamed.md"


def test_case_insensitive_requires_path_norm(tmp_path: Path) -> None:
    a = _mk_snapshot(tmp_path, "VOL_N", "20260101T000000Z",
                     [("x.txt", "file", 1, 1)], with_path_norm=False)
    b = _mk_snapshot(tmp_path, "VOL_N", "20260202T000000Z",
                     [("x.txt", "file", 1, 1)])
    with pytest.raises(DiffError) as ei:
        materialize_diff(tmp_path, a, b, options={"case_insensitive": True})
    assert "path_norm" in str(ei.value)
    # B 侧缺失同样报错
    with pytest.raises(DiffError):
        materialize_diff(tmp_path, b, a, options={"case_insensitive": True})


def test_case_insensitive_duplicate_folded_names(tmp_path: Path) -> None:
    """同目录大小写不同名（Linux 合法）：组内按 path 序一对一配对，多余归 removed。"""
    a = _mk_snapshot(tmp_path, "VOL_D", "20260101T000000Z", [
        ("docs/a.txt", "file", 1, 1),
        ("docs/A.txt", "file", 2, 2),
    ])
    b = _mk_snapshot(tmp_path, "VOL_D", "20260202T000000Z", [
        ("docs/a.txt", "file", 1, 1),
    ])
    r = materialize_diff(tmp_path, a, b, options={"case_insensitive": True})
    assert r.counts["identical"] == 2        # root + docs + 配对的 docs/a.txt 之一
    assert r.counts["removed"] == 1 and r.counts["added"] == 0


# ---------------------------------------------------------------- evidence


def test_evidence_no_hash(tmp_path: Path, base_snaps) -> None:
    a, b = base_snaps
    r = materialize_diff(tmp_path, a, b)
    assert r.evidence_level == "size+mtime"
    assert r.evidence == {
        "content_changed": "unavailable",
        "moved_or_renamed": "unavailable",
        "size_changed": "size",
        "mtime_changed": "mtime",
        "type_changed": "type",
        "identical": "size+mtime",
    }
    # 落库可读（read_evidence）
    level, ev = _read_evidence(r.db_path)
    assert level == "size+mtime" and ev == r.evidence


def test_evidence_with_hash(tmp_path: Path) -> None:
    hx = "ab" * 32
    a = _mk_snapshot(tmp_path, "VOL_H", "20260101T000000Z", [
        ("f1.txt", "file", 1, 1),
        ("f2.txt", "file", 2, 2),
    ], hashes={"f1.txt": (hx, "full"), "f2.txt": (hx, "full")})
    b = _mk_snapshot(tmp_path, "VOL_H", "20260202T000000Z", [
        ("f1.txt", "file", 1, 1),
        ("f2.txt", "file", 2, 3),
    ], hashes={"f1.txt": (hx, "full"), "f2.txt": (hx + "cd", "full")})
    r = materialize_diff(tmp_path, a, b, options={"hash": "sha256"})
    assert r.evidence_level == "hash"
    assert r.evidence["content_changed"] == "hash"
    assert r.evidence["moved_or_renamed"] == "hash"
    assert r.evidence["identical"] == "hash"
    assert r.counts["content_changed"] == 1
    assert r.counts["identical"] == 2        # root（无哈希）+ f1.txt（哈希相同）


def test_evidence_hash_enabled_but_unavailable(tmp_path: Path, base_snaps) -> None:
    """hash 启用但两侧均无 full 哈希 → content_changed 证据 unavailable，整体退
    回 size+mtime。"""
    a, b = base_snaps
    r = materialize_diff(tmp_path, a, b, options={"hash": "sha256"})
    assert r.evidence_level == "size+mtime"
    assert r.evidence["content_changed"] == "unavailable"


# ---------------------------------------------------------------- 回归 + API/CLI


def test_default_options_regression(tmp_path: Path, base_snaps) -> None:
    """默认参数与显式全 false 结果完全一致（diff_id 与 counts）。"""
    a, b = base_snaps
    r_default = materialize_diff(tmp_path, a, b)
    r_false = materialize_diff(tmp_path, a, b, options={
        "case_insensitive": False, "ignore_mtime": False,
        "ignore_size": False, "show_identical": False})
    assert r_default.diff_id == r_false.diff_id
    assert r_default.counts == r_false.counts


def test_api_diff_options(tmp_path: Path, base_snaps) -> None:
    from fastapi.testclient import TestClient
    from cold_manifest.server import create_app

    a, b = base_snaps
    with TestClient(create_app(data_root=str(tmp_path))) as client:
        body = {"a": a, "b": b, "ignore_mtime": True, "show_identical": True}
        resp = client.post("/api/diffs", json=body)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["counts"]["mtime_changed"] == 0
        assert data["counts"]["identical"] > 0
        assert data["evidence_level"] == "size+mtime"
        assert data["evidence"]["content_changed"] == "unavailable"

        # 未知选项 → 400
        resp = client.post("/api/diffs", json={"a": a, "b": b,
                                               "options": {"nope": 1}})
        assert resp.status_code == 400

        # case_insensitive 遇 NULL path_norm → 400
        a_nn = _mk_snapshot(tmp_path, "VOL_NN", "20260101T000000Z",
                            [("x.txt", "file", 1, 1)], with_path_norm=False)
        b_nn = _mk_snapshot(tmp_path, "VOL_NN", "20260202T000000Z",
                            [("x.txt", "file", 1, 1)])
        resp = client.post("/api/diffs", json={"a": a_nn, "b": b_nn,
                                               "case_insensitive": True})
        assert resp.status_code == 400
        assert "path_norm" in resp.json()["detail"]

        # summary / detail 带 evidence
        resp = client.get(f"/api/diffs/{data['diff_id']}/summary")
        assert resp.status_code == 200
        assert resp.json()["evidence_level"] == "size+mtime"
        resp = client.get(f"/api/diffs/{data['diff_id']}")
        assert resp.json()["evidence"]["identical"] == "size+mtime"


def test_cli_diff_switches(tmp_path: Path, base_snaps, capsys) -> None:
    a, b = base_snaps
    rc = cli_main(["diff", a, b, "--data-root", str(tmp_path),
                   "--ignore-mtime", "--show-identical",
                   "--output", str(tmp_path / "d.csv")])
    assert rc == 1        # 仍有 size/type/removed/added 差异
    out = capsys.readouterr().out
    assert "证据等级" in out and "size+mtime" in out
    lines = (tmp_path / "d.csv").read_text(encoding="utf-8").splitlines()[1:]
    cats = {ln.split(",")[0] for ln in lines}
    assert "mtime_changed" not in cats
    assert "identical" in cats          # --show-identical 落 CSV
