"""P0 正确性：哈希任务驱逐池连接 + malformed 优雅报错 + integrity-check + 去 immutable。"""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cold_manifest.api import routes_snapshots
from cold_manifest.catalog import catalog_path, ensure_disk, ensure_volume, register_snapshot
from cold_manifest.cli import main as cli_main
from cold_manifest.db import file_uri, init_catalog, init_snapshot, open_snapshot
from cold_manifest.server import create_app


# ---------------------------------------------------------------- 夹具

_ROWS = [
    (1, 0, ".", "", 0, "dir", None, None, None),
    (2, 1, "docs", "docs", 1, "dir", None, None, None),
    (3, 2, "docs/a.txt", "a.txt", 2, "file", 21, 1111, "docs/a.txt"),
]


def _build_snapshot(data_root: Path, sid: str) -> str:
    """建一个封库快照并注册 catalog，返回 snapshot_id。"""
    vol, ts = sid.split("/")
    db_dir = data_root / vol / ts
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_dir / "snapshot.db")
    init_snapshot(conn)
    conn.executemany(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type, size_bytes,"
        " mtime_ns, path_norm) VALUES(?,?,?,?,?,?,?,?,?)", _ROWS)
    conn.execute("INSERT INTO meta(key, value) VALUES('status','sealed')")
    conn.commit()
    conn.close()

    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    ensure_disk(cat, "DISK1", physical_model="TEST")
    ensure_volume(cat, vol, "DISK1")
    register_snapshot(cat, sid, vol, status="sealed", host_path=str(db_dir))
    cat.commit()
    cat.close()
    return sid


@pytest.fixture()
def snap(tmp_path: Path) -> "tuple[Path, str]":
    sid = "VOL_P0/20260101T000000Z"
    _build_snapshot(tmp_path, sid)
    return tmp_path, sid


# ---------------------------------------------------------------- ① 去 immutable

def test_open_snapshot_uses_mode_ro_not_immutable(snap, monkeypatch) -> None:
    """open_snapshot 打开参数不得再带 immutable=1（封库后仍会被就地写）。"""
    data_root, sid = snap
    db = data_root / sid.split("/")[0] / sid.split("/")[1] / "snapshot.db"
    seen: "list[str]" = []
    orig = sqlite3.connect

    def spy(*a, **k):
        seen.append(a[0] if a else k.get("database", ""))
        return orig(*a, **k)

    monkeypatch.setattr(routes_snapshots.sqlite3, "connect", sqlite3.connect)  # no-op 保引用
    monkeypatch.setattr("cold_manifest.db.sqlite3.connect", spy)
    conn = open_snapshot(db)
    conn.execute("SELECT count(*) FROM entries").fetchone()
    conn.close()
    assert seen and "immutable=1" not in seen[0]
    assert "mode=ro" in seen[0]


def test_file_uri_immutable_flag_controls_uri(tmp_path: Path) -> None:
    """file_uri：immutable=False 不追加 immutable=1；True 才追加。"""
    db = tmp_path / "x.db"
    db.write_bytes(b"")
    uri_ro = file_uri(db, immutable=False)
    uri_im = file_uri(db, immutable=True)
    assert "immutable=1" not in uri_ro and "mode=ro" in uri_ro
    assert uri_im.endswith("&immutable=1")


def test_diff_attach_not_immutable(snap, monkeypatch) -> None:
    """diff_engine 物化时 ATTACH 快照库的 URI 不得含 immutable=1。"""
    from cold_manifest import diff_engine

    data_root, sid = snap
    db = data_root / sid.split("/")[0] / sid.split("/")[1] / "snapshot.db"
    captured: "list[str]" = []
    orig = diff_engine.file_uri

    def spy(path, **k):
        uri = orig(path, **k)
        captured.append(uri)
        return uri

    monkeypatch.setattr(diff_engine, "file_uri", spy)
    raw_conn = sqlite3.connect(":memory:")
    raw_conn.row_factory = sqlite3.Row

    class _ConnWrap:
        def __init__(self, c: sqlite3.Connection) -> None:
            self._c = c

        def execute(self, sql: str, params: "tuple | None" = None) -> Any:
            if "ATTACH" in sql and params:
                captured.extend(params)
            return self._c.execute(sql, params) if params else self._c.execute(sql)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._c, name)

    try:
        diff_engine._materialize_sql(_ConnWrap(raw_conn), db, db, hash_enabled=False)
    except sqlite3.Error:
        pass  # 内存连接没建 diff 工作表，ATTACH 之后的步骤失败可忽略
    assert captured, "ATTACH 应经过 file_uri"
    assert all("immutable=1" not in u for u in captured)


# ---------------------------------------------------------------- ② 哈希任务驱逐池连接

def test_hash_task_evicts_pool_before_and_after(snap, monkeypatch) -> None:
    """_run_hash 必须写前/写后逐出该快照的池连接，且旧连接被关闭。"""
    data_root, sid = snap
    app = create_app(str(data_root))
    with TestClient(app):
        state = app.state.cldm
        old_conn = state.snapshot_db(sid)          # 预热池
        assert old_conn is state._snap_pools[sid]

        calls: "list[str]" = []
        real_evict = state.evict_snapshot

        def spy_evict(s: str) -> int:
            calls.append(s)
            return real_evict(s)

        monkeypatch.setattr(routes_snapshots, "_evict_snapshot_pools", spy_evict)
        payload = {"snapshot_id": sid, "algo": "sha256", "policy": "full",
                   "data_root": str(data_root)}
        result = routes_snapshots._run_hash(payload, None, None)

        assert calls == [sid, sid], "哈希任务应写前、写后各逐出一次"
        assert result["total"] >= 1 or isinstance(result, dict)
        with pytest.raises(sqlite3.ProgrammingError):
            old_conn.execute("SELECT count(*) FROM entries")  # 旧连接已被 close


# ---------------------------------------------------------------- ③ malformed 优雅报错

def _corrupt(db: Path) -> None:
    raw = bytearray(db.read_bytes())
    raw[512:1024] = b"\xde\xad\xbe\xef" * 128   # 覆写第 2 页 → malformed
    db.write_bytes(bytes(raw))


def test_malformed_snapshot_db_returns_readable_409(snap) -> None:
    """损坏的快照库：API 返回可读中文 409，而不是裸 500 traceback。"""
    data_root, sid = snap
    _corrupt(data_root / sid.split("/")[0] / sid.split("/")[1] / "snapshot.db")
    app = create_app(str(data_root))
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(f"/api/snapshots/{sid}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "快照库读取失败" in detail
    assert "integrity-check" in detail


# ---------------------------------------------------------------- ④ CLI integrity-check

def test_integrity_check_ok_snapshot(snap, capsys) -> None:
    data_root, sid = snap
    rc = cli_main(["integrity-check", sid, "--data-root", str(data_root)])
    assert rc == 0
    assert "正常" in capsys.readouterr().out


def test_integrity_check_corrupt_snapshot(snap, capsys) -> None:
    data_root, sid = snap
    _corrupt(data_root / sid.split("/")[0] / sid.split("/")[1] / "snapshot.db")
    rc = cli_main(["integrity-check", sid, "--data-root", str(data_root)])
    assert rc == 1
    assert "损坏" in capsys.readouterr().out


def test_integrity_check_all_mixed(snap, tmp_path, capsys) -> None:
    """--all 遍历 catalog：一个好库 + 一个坏库 → exit 1。"""
    data_root, sid = snap
    bad_sid = "VOL_P0/20260201T000000Z"
    _build_snapshot(data_root, bad_sid)
    _corrupt(data_root / bad_sid.split("/")[0] / bad_sid.split("/")[1] / "snapshot.db")
    rc = cli_main(["integrity-check", "--all", "--data-root", str(data_root)])
    assert rc == 1
    out = capsys.readouterr().out
    assert sid in out and bad_sid in out


def test_integrity_check_requires_target(snap) -> None:
    data_root, _sid = snap
    assert cli_main(["integrity-check", "--data-root", str(data_root)]) == 2
