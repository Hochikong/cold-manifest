"""快照删除（P4-①）：API DELETE + catalog.delete_snapshot + CLI 子命令。"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import (
    catalog_path,
    delete_snapshot,
    ensure_disk,
    ensure_volume,
    register_snapshot,
)
from cold_manifest.cli import main as cli_main
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.lockfile import DataRootLock
from cold_manifest.server import create_app

SID = "vol/20260101T000000Z"
TS = "20260101T000000Z"


# ---------------------------------------------------------------- fixtures


def _build_snapshot_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    init_snapshot(conn)
    conn.executemany(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type,"
        " size_bytes, mtime_ns, ext) VALUES(?,?,?,?,?,?,?,?,?)",
        [
            (1, 0, ".", "", 0, "dir", None, None, ""),
            (2, 1, "a.txt", "a.txt", 1, "file", 1000, 100, ".txt"),
            (3, 1, "sub", "sub", 1, "dir", None, None, ""),
            (4, 3, "sub/b.bin", "b.bin", 2, "file", 5000, 200, ".bin"),
        ],
    )
    conn.executemany("INSERT INTO meta(key, value) VALUES(?,?)",
                     [("status", "sealed"), ("scan_root", "/scan"), ("root_path", "/scan")])
    conn.commit()
    conn.close()


class Env:
    """data_root + 已注册快照 + 可选盘上副本/scan_root 的测试环境。"""

    def __init__(self, tmp_path: Path) -> None:
        self.data_root = tmp_path / "data"
        self.scan_root = tmp_path / "scan"
        self.host_dir = self.data_root / "vol" / TS
        _build_snapshot_db(self.host_dir / "snapshot.db")
        # 模拟扫描源内容（主机目录外）
        (self.scan_root / "docs").mkdir(parents=True)
        (self.scan_root / "docs" / "f.txt").write_text("x" * 100)
        # 盘上副本
        self.copy_dir = self.scan_root / "_coldmanifest" / "vol" / TS
        self.copy_dir.mkdir(parents=True)
        (self.copy_dir / "snapshot.db").write_bytes(b"copydb")
        (self.copy_dir / "snapshot.json").write_text("{}")

        cat = sqlite3.connect(catalog_path(self.data_root))
        cat.row_factory = sqlite3.Row
        init_catalog(cat)
        ensure_disk(cat, "DISK1")
        ensure_volume(cat, "vol", "DISK1")
        register_snapshot(cat, SID, "vol", status="sealed", host_path=str(self.scan_root),
                          file_count=2, dir_count=1, total_bytes=6000)
        cat.execute(
            "INSERT INTO on_disk_copies(snapshot_id, disk_path, status) VALUES(?,?,?)",
            (SID, str(self.copy_dir / "snapshot.db"), "ok"))
        cat.commit()
        self.cat = cat

    def client(self) -> TestClient:
        return TestClient(create_app(data_root=str(self.data_root)))


@pytest.fixture()
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# ---------------------------------------------------------------- 正常删除


def test_delete_basic(env: Env) -> None:
    with env.client() as c:
        r = c.delete(f"/api/snapshots/{SID}")
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["snapshot_id"] == SID
    assert body["deleted_host"] is True
    assert body["deleted_disk"] is None          # keep：未处理
    assert body["freed_bytes"] > 0
    assert body["warnings"] == []
    # 主机目录整个消失（含 snapshot.db / scan_journal 等）
    assert not env.host_dir.exists()
    # 盘上副本保留
    assert env.copy_dir.exists()
    # catalog：snapshots / on_disk_copies 行没了，disks/volumes 保留
    assert env.cat.execute("SELECT 1 FROM snapshots WHERE snapshot_id=?", (SID,)).fetchone() is None
    assert env.cat.execute("SELECT 1 FROM on_disk_copies WHERE snapshot_id=?", (SID,)).fetchone() is None
    assert env.cat.execute("SELECT 1 FROM volumes WHERE volume_id='vol'").fetchone()
    assert env.cat.execute("SELECT 1 FROM disks WHERE disk_id='DISK1'").fetchone()
    # 再删一次 → 404（幂等语义）
    with env.client() as c:
        assert c.delete(f"/api/snapshots/{SID}").status_code == 404


def test_delete_on_disk(env: Env) -> None:
    with env.client() as c:
        r = c.delete(f"/api/snapshots/{SID}", params={"on_disk": "delete"})
        assert r.status_code == 200
        body = r.json()
    assert body["deleted_disk"] is True
    assert not env.copy_dir.exists()
    # 上层 _coldmanifest/vol 结构保留（不影响同卷其他快照）
    assert (env.scan_root / "_coldmanifest" / "vol").is_dir()


def test_delete_on_disk_without_scan_root_warns(env: Env) -> None:
    # 快照库 meta 无 scan_root/root_path，且盘上副本文件已被外部清理 → warning 不失败
    db = env.host_dir / "snapshot.db"
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM meta WHERE key IN ('scan_root','root_path')")
    conn.commit()
    conn.close()
    import shutil
    shutil.rmtree(env.copy_dir)
    with env.client() as c:
        r = c.delete(f"/api/snapshots/{SID}", params={"on_disk": "delete"})
        assert r.status_code == 200
        body = r.json()
    assert body["deleted_disk"] is False
    assert any("盘上副本" in w for w in body["warnings"])
    assert body["deleted_host"] is True


# ---------------------------------------------------------------- diff 引用


def _add_diff_ref(env: Env) -> Path:
    diff_id = "dif_20260201T000000Z"
    env.cat.execute(
        "INSERT INTO diff_runs(diff_id, a, b, options_hash, created_at, status)"
        " VALUES(?,?,?,?,?,?)", (diff_id, SID, "vol/20260201T000000Z", "x", "now", "done"))
    env.cat.commit()
    db = env.data_root / "_diffs" / f"{diff_id}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"diffdb")
    return db


def test_delete_referenced_by_diff_409(env: Env) -> None:
    _add_diff_ref(env)
    with env.client() as c:
        r = c.delete(f"/api/snapshots/{SID}")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "dif_20260201T000000Z" in str(detail)
    # 快照未被删
    assert env.host_dir.exists()


def test_delete_force_cascades(env: Env) -> None:
    db = _add_diff_ref(env)
    with env.client() as c:
        r = c.delete(f"/api/snapshots/{SID}", params={"force": "true"})
        assert r.status_code == 200
        body = r.json()
    assert body["diffs_removed"] == ["dif_20260201T000000Z"]
    assert not db.exists()
    assert env.cat.execute("SELECT 1 FROM diff_runs").fetchone() is None
    assert not env.host_dir.exists()


# ---------------------------------------------------------------- 活跃任务


def test_delete_with_active_task_409(env: Env) -> None:
    # 在 app 启动后插入活跃任务（lifespan 启动对账会把遗留 running 行置 error）
    with env.client() as c:
        env.cat.execute(
            "INSERT INTO tasks(task_id, kind, payload_json, status, created_at)"
            " VALUES(?,?,?,?,?)",
            ("t1", "collect", '{"volume_id": "vol", "path": "/scan"}', "running", "now"))
        env.cat.commit()
        r = c.delete(f"/api/snapshots/{SID}")
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert "t1" in str(detail)
        assert env.host_dir.exists()
        env.cat.execute("DELETE FROM tasks WHERE task_id='t1'")
        env.cat.commit()
        # 任务终态后可删
        env.cat.execute(
            "INSERT INTO tasks(task_id, kind, payload_json, status, created_at)"
            " VALUES(?,?,?,?,?)", ("t2", "hash", f'{{"snapshot_id": "{SID}"}}', "error", "now"))
        env.cat.commit()
        assert c.delete(f"/api/snapshots/{SID}").status_code == 200


# ---------------------------------------------------------------- 404 / 400 / 越界


def test_delete_unknown_404(env: Env) -> None:
    with env.client() as c:
        assert c.delete("/api/snapshots/nope/123").status_code == 404


def test_delete_bad_on_disk_400(env: Env) -> None:
    with env.client() as c:
        assert c.delete(f"/api/snapshots/{SID}",
                        params={"on_disk": "nuke"}).status_code in (400, 422)


def test_delete_path_traversal(env: Env, tmp_path: Path) -> None:
    """恶意 snapshot_id 不得删到 <data_root>/<volume_id> 之外的目录。"""
    treasure = tmp_path / "precious"
    treasure.mkdir()
    (treasure / "keep.txt").write_text("keep me")

    with env.client() as c:
        # 快照须已在 catalog 注册才会走到删除步骤
        register_snapshot(env.cat, "vol/..%2F..%2Fprecious", "vol", status="sealed",
                          host_path=str(treasure))
        env.cat.commit()
        register_snapshot(env.cat, "vol/../..", "vol", status="sealed",
                          host_path=str(treasure))
        env.cat.commit()
        for sid in ("vol/../../precious", "vol/..", "vol/", "../precious"):
            r = c.delete(f"/api/snapshots/{sid}")
            assert r.status_code in (400, 404, 405, 422), (sid, r.status_code)

    assert (treasure / "keep.txt").exists()
    # 合法快照仍完好
    assert (env.host_dir / "snapshot.db").exists()


def test_delete_direct_traversal_safety(env: Env, tmp_path: Path) -> None:
    """绕过路由直接调 delete_snapshot：ts 含 .. 时被 _safe_child 拦截。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    # 注册一个能通过 catalog 存在性检查的恶意 sid（volume_id 校验合法、ts 越界）
    register_snapshot(env.cat, "vol/../../outside", "vol", status="sealed",
                      host_path=str(outside))
    env.cat.commit()
    with pytest.raises(Exception) as ei:
        delete_snapshot(env.cat, env.data_root, "vol/../../outside")
    assert "越界" in str(ei.value)
    assert (outside / "f.txt").exists()


def test_delete_lock_busy(env: Env, monkeypatch) -> None:
    monkeypatch.delenv("CLDM_SKIP_LOCK", raising=False)
    with DataRootLock(env.data_root):
        with env.client() as c:
            r = c.delete(f"/api/snapshots/{SID}")
    assert r.status_code == 409
    assert "占用" in r.json()["detail"]


# ---------------------------------------------------------------- catalog 直调


def test_delete_direct_function(env: Env) -> None:
    res = delete_snapshot(env.cat, env.data_root, SID, on_disk="delete")
    assert res["deleted_host"] and res["deleted_disk"]
    assert not env.host_dir.exists() and not env.copy_dir.exists()


# ---------------------------------------------------------------- CLI


def test_cli_delete_ok(env: Env, capsys, monkeypatch) -> None:
    monkeypatch.delenv("CLDM_SKIP_LOCK", raising=False)
    rc = cli_main(["delete", SID, "--data-root", str(env.data_root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "已删除快照" in out and "已删除" in out
    assert not env.host_dir.exists()
    assert env.copy_dir.exists()


def test_cli_delete_on_disk_flag(env: Env, capsys) -> None:
    rc = cli_main(["delete", SID, "--data-root", str(env.data_root),
                   "--on-disk", "delete", "--force"])
    out = capsys.readouterr().out
    assert rc == 0
    assert not env.copy_dir.exists()


def test_cli_delete_missing_404_code2(env: Env, capsys) -> None:
    rc = cli_main(["delete", "nope/123", "--data-root", str(env.data_root)])
    out = capsys.readouterr()
    assert rc == 2
    assert "快照不存在" in out.err


def test_cli_delete_diff_blocked_code2(env: Env, capsys) -> None:
    _add_diff_ref(env)
    rc = cli_main(["delete", SID, "--data-root", str(env.data_root)])
    out = capsys.readouterr()
    assert rc == 2
    assert "dif_20260201T000000Z" in out.err
    assert env.host_dir.exists()
    # --force 放行
    rc = cli_main(["delete", SID, "--data-root", str(env.data_root), "--force"])
    assert rc == 0


def test_delete_invalid_volume_id_400(env: Env) -> None:
    """非法 volume_id（`..`、编码字符等）→ SnapshotDeleteError/400，不得裸抛成 500。"""
    from cold_manifest.catalog import SnapshotDeleteError

    for bad in ("../evil", "..%2Fevil", "-lead/x", "a..b"):
        with pytest.raises(SnapshotDeleteError):
            delete_snapshot(env.cat, env.data_root, bad)

    with env.client() as c:
        r = c.delete("/api/snapshots/..%2Fevil")
        assert r.status_code == 400, (r.status_code, r.text)
        # 合法快照不受影响
        assert c.delete(f"/api/snapshots/{SID}").status_code == 200
