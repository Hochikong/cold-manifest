"""按需哈希引擎测试（§4.5）：full/sampled 语义、缓存复用、续算、错误、API、CLI。"""

import hashlib
import os
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import catalog_path, ensure_disk, ensure_volume, register_snapshot
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.hash import HashError, hash_file, hash_snapshot
from cold_manifest.server import create_app


# ---------------------------------------------------------------- fixtures

A_TXT = b"hello cold manifest\n"
B_BIN = bytes(range(256)) * 40  # 10240 B，含全部字节值


def _build_snapshot(data_root: Path, host: Path, sid_ts: str, rows: "list[tuple]") -> str:
    """建一个封库快照（entries 行自定）并注册 catalog，返回 snapshot_id。"""
    db_dir = data_root / sid_ts.split("/")[0] / sid_ts.split("/")[1]
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_dir / "snapshot.db")
    init_snapshot(conn)
    conn.executemany(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type, size_bytes,"
        " mtime_ns, path_norm) VALUES(?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.execute("INSERT INTO meta(key, value) VALUES('status','sealed')")
    conn.commit()
    conn.close()

    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    ensure_disk(cat, "DISK1", physical_model="TEST")
    ensure_volume(cat, sid_ts.split("/")[0], "DISK1")
    register_snapshot(cat, sid_ts, sid_ts.split("/")[0], status="sealed", host_path=str(host))
    cat.commit()
    cat.close()
    return sid_ts


def _std_rows() -> "list[tuple]":
    """root + docs/a.txt(100, 1111) + docs/b.bin(10240, 2222)。"""
    return [
        (1, 0, ".", "", 0, "dir", None, None, None),
        (2, 1, "docs", "docs", 1, "dir", None, None, None),
        (3, 2, "docs/a.txt", "a.txt", 2, "file", len(A_TXT), 1111, "docs/a.txt"),
        (4, 2, "docs/b.bin", "b.bin", 2, "file", len(B_BIN), 2222, "docs/b.bin"),
    ]


@pytest.fixture()
def env(tmp_path: Path) -> "tuple[Path, Path, str]":
    """data_root + 源目录（真实文件，mtime 显式设定以匹配 entries 行）+ snapshot_id。"""
    data_root = tmp_path / "data"
    host = tmp_path / "host"
    (host / "docs").mkdir(parents=True)
    (host / "docs" / "a.txt").write_bytes(A_TXT)
    (host / "docs" / "b.bin").write_bytes(B_BIN)
    os_mtimes = {3: 1111, 4: 2222}
    for entry_id, mtime in os_mtimes.items():
        p = (host / "docs" / ("a.txt" if entry_id == 3 else "b.bin"))
        os.utime(p, ns=(mtime, mtime))
    sid = _build_snapshot(data_root, host, "vol/20260101T000000Z", _std_rows())
    return data_root, host, sid


def _open_rw(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _snap_db(data_root: Path, sid: str) -> Path:
    return data_root / sid.split("/")[0] / sid.split("/")[1] / "snapshot.db"


def _hash_rows(conn: sqlite3.Connection) -> "dict[int, sqlite3.Row]":
    return {r["entry_id"]: r for r in conn.execute(
        "SELECT entry_id, hash_algo, hash_hex, hash_state FROM entries WHERE type='file'")}


# ---------------------------------------------------------------- hash_file


def test_hash_file_full_matches_hashlib(tmp_path: Path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(A_TXT)
    assert hash_file(p, algo="sha256", policy="full") == hashlib.sha256(A_TXT).hexdigest()


def test_hash_file_missing_returns_none(tmp_path: Path) -> None:
    assert hash_file(tmp_path / "nope.bin") is None


def test_hash_sampled_semantics(tmp_path: Path) -> None:
    # 大文件（> 2×sample_bytes）：sampled 与 full 不同；改中段字节（大小不变）不影响 sampled
    big = tmp_path / "big.bin"
    payload = bytearray(b"\x00" * (65536 * 4))
    big.write_bytes(payload)
    sampled1 = hash_file(big, policy="sampled")
    full = hash_file(big, policy="full")
    assert sampled1 != full
    payload[65536 * 2] = 0xFF  # 中段
    big.write_bytes(payload)
    assert hash_file(big, policy="sampled") == sampled1  # 首尾未变 + 大小未变
    assert hash_file(big, policy="full") != full

    # 小文件（≤ 2×sample）：sampled 退化为整文件，但混入了大小标记 → 与 full 不同
    small = tmp_path / "small.bin"
    small.write_bytes(A_TXT)
    assert hash_file(small, policy="sampled") != hash_file(small, policy="full")
    # 大小标记生效：同内容不同大小 → 不同 sampled 值
    small2 = tmp_path / "small2.bin"
    small2.write_bytes(A_TXT + b"x")
    assert hash_file(small2, policy="sampled") != hash_file(small, policy="sampled")


# ---------------------------------------------------------------- hash_snapshot


def test_hash_snapshot_computes_and_persists(env) -> None:
    data_root, _, sid = env
    conn = _open_rw(_snap_db(data_root, sid))
    result = hash_snapshot(conn, data_root, sid)
    assert result["total"] == 2 and result["computed"] == 2 and result["cached"] == 0
    assert result["errors"] == 0
    rows = _hash_rows(conn)
    assert rows[3]["hash_state"] == "full"
    assert rows[3]["hash_hex"] == hashlib.sha256(A_TXT).hexdigest()
    assert rows[4]["hash_hex"] == hashlib.sha256(B_BIN).hexdigest()
    assert rows[4]["hash_algo"] == "sha256"
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    assert meta["hash_policy"] == "full" and meta["hash_algo"] == "sha256"
    conn.close()


def test_hash_snapshot_cache_reuse_across_snapshots(env, tmp_path) -> None:
    data_root, host, sid = env
    conn = _open_rw(_snap_db(data_root, sid))
    hash_snapshot(conn, data_root, sid)
    conn.close()

    # 第二个快照：同 (size, mtime, path_norm) 的 entries → 全部命中 cached
    sid2 = _build_snapshot(data_root, host, "vol/20260102T000000Z", _std_rows())
    conn2 = _open_rw(_snap_db(data_root, sid2))
    result = hash_snapshot(conn2, data_root, sid2)
    assert result["cached"] == 2 and result["computed"] == 0
    rows = _hash_rows(conn2)
    # 缓存命中写实际策略，不再写废弃的 'cached'
    assert rows[3]["hash_state"] == "full"
    assert rows[3]["hash_hex"] == hashlib.sha256(A_TXT).hexdigest()
    conn2.close()


def test_hash_snapshot_cache_hit_records_actual_policy(env, tmp_path) -> None:
    """sampled 哈希的缓存命中 → hash_state='sampled'（非 full、非废弃 cached）。"""
    data_root, host, sid = env
    conn = _open_rw(_snap_db(data_root, sid))
    hash_snapshot(conn, data_root, sid, policy="sampled")
    conn.close()
    sid2 = _build_snapshot(data_root, host, "vol/20260102T000000Z", _std_rows())
    conn2 = _open_rw(_snap_db(data_root, sid2))
    result = hash_snapshot(conn2, data_root, sid2, policy="sampled")
    assert result["cached"] == 2
    rows = _hash_rows(conn2)
    assert rows[3]["hash_state"] == "sampled"
    conn2.close()

    # 旧缓存行（policy 为 NULL）→ 回退本次请求的 policy
    cat = sqlite3.connect(catalog_path(data_root))
    cat.execute("UPDATE hash_cache SET policy=NULL")
    cat.commit()
    cat.close()
    sid3 = _build_snapshot(data_root, host, "vol/20260103T000000Z", _std_rows())
    conn3 = _open_rw(_snap_db(data_root, sid3))
    result = hash_snapshot(conn3, data_root, sid3, policy="full")
    assert result["cached"] == 2
    rows = _hash_rows(conn3)
    assert rows[3]["hash_state"] == "full"
    conn3.close()


def test_ensure_disk_no_extra_fields(tmp_path) -> None:
    """ensure_disk 无附加字段也不产生残缺 SQL（rstrip(', ') 防御）。"""
    cat = sqlite3.connect(catalog_path(tmp_path))
    init_catalog(cat)
    ensure_disk(cat, "DISK_X")
    ensure_disk(cat, "DISK_X")  # 幂等
    row = cat.execute(
        "SELECT disk_id, last_seen IS NOT NULL FROM disks WHERE disk_id='DISK_X'"
    ).fetchone()
    cat.close()
    assert row[0] == "DISK_X" and row[1] == 1


def test_hash_snapshot_resumable(env) -> None:
    data_root, _, sid = env
    conn = _open_rw(_snap_db(data_root, sid))
    r1 = hash_snapshot(conn, data_root, sid, limit=1)
    assert r1["computed"] == 1
    rows = _hash_rows(conn)
    done_ids = [i for i, r in rows.items() if r["hash_hex"]]
    assert len(done_ids) == 1

    r2 = hash_snapshot(conn, data_root, sid)
    assert r2["total"] == 1 and r2["computed"] == 1  # 只剩未算的
    rows = _hash_rows(conn)
    assert all(r["hash_hex"] for r in rows.values())
    assert rows[3]["hash_hex"] == hashlib.sha256(A_TXT).hexdigest()
    conn.close()


def test_hash_snapshot_cancel_before_start(env) -> None:
    """取消事件已置位：不写入任何哈希，现场保留（可续算）。"""
    import threading

    data_root, _, sid = env
    conn = _open_rw(_snap_db(data_root, sid))
    ev = threading.Event()
    ev.set()
    with pytest.raises(RuntimeError, match="取消"):
        hash_snapshot(conn, data_root, sid, cancel_event=ev)
    rows = _hash_rows(conn)
    assert all(r["hash_hex"] is None for r in rows.values())  # 未写入任何哈希
    conn.close()


def test_hash_snapshot_error_entries(env, tmp_path) -> None:
    data_root, host, sid = env
    # 补一个指向缺失文件的条目
    conn = _open_rw(_snap_db(data_root, sid))
    conn.execute(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type, size_bytes,"
        " mtime_ns, path_norm) VALUES(9, 2, 'docs/ghost.txt', 'ghost.txt', 2, 'file',"
        " 5, 3333, 'docs/ghost.txt')")
    conn.commit()
    result = hash_snapshot(conn, data_root, sid)
    assert result["errors"] == 1 and result["computed"] == 2
    row = conn.execute("SELECT * FROM entries WHERE entry_id=9").fetchone()
    assert row["hash_state"] == "error"
    assert "hash:" in (row["error"] or "")
    # 续算不重试 error 条目
    r2 = hash_snapshot(conn, data_root, sid)
    assert r2["total"] == 0
    conn.close()


def test_hash_snapshot_rejects_bad_args(env) -> None:
    data_root, _, sid = env
    conn = _open_rw(_snap_db(data_root, sid))
    with pytest.raises(HashError):
        hash_snapshot(conn, data_root, sid, algo="md5")
    with pytest.raises(HashError):
        hash_snapshot(conn, data_root, sid, policy="half")
    conn.close()


def test_hash_snapshot_unregistered_snapshot(tmp_path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    cat.commit()
    cat.close()
    # 未注册：hash_snapshot 应在 resolve host_path 阶段失败（conn 尚未被触碰）
    with pytest.raises(HashError, match="未注册"):
        hash_snapshot(None, data_root, "nope/20260101T000000Z")  # type: ignore[arg-type]


def test_hash_snapshot_host_path_unavailable(env) -> None:
    data_root, _, sid = env
    cat = sqlite3.connect(catalog_path(data_root))
    cat.execute("UPDATE snapshots SET host_path='/nonexistent/__no_such_dir__'")
    cat.commit()
    cat.close()
    conn = _open_rw(_snap_db(data_root, sid))
    with pytest.raises(HashError, match="不可用"):
        hash_snapshot(conn, data_root, sid)
    conn.close()


# ---------------------------------------------------------------- API


@pytest.fixture()
def client(env) -> TestClient:
    data_root, _, _ = env
    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c


def _wait_task(c: TestClient, task_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = c.get(f"/api/tasks/{task_id}").json()
        if body["status"] in ("done", "error", "cancelled"):
            return body
        time.sleep(0.05)
    raise TimeoutError(task_id)


def test_api_hash_submit_and_done(client, env) -> None:
    _, _, sid = env
    r = client.post(f"/api/snapshots/{sid}/hash", json={})
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    task = _wait_task(client, body["task_id"])
    assert task["status"] == "done"
    assert task["result"]["computed"] == 2
    assert task["result"]["policy"] == "full"
    # payload 契约
    assert task["payload"]["snapshot_id"] == sid
    assert task["payload"]["algo"] == "sha256"


def test_api_hash_sampled_policy(client, env) -> None:
    _, _, sid = env
    r = client.post(f"/api/snapshots/{sid}/hash", json={"policy": "sampled"})
    assert r.status_code == 201
    task = _wait_task(client, r.json()["task_id"])
    assert task["status"] == "done"
    assert task["result"]["policy"] == "sampled"


def test_api_hash_404(client) -> None:
    r = client.post("/api/snapshots/nope/123/hash", json={})
    assert r.status_code == 404


def test_api_hash_400_bad_policy(client, env) -> None:
    _, _, sid = env
    assert client.post(f"/api/snapshots/{sid}/hash", json={"policy": "half"}).status_code == 400
    assert client.post(f"/api/snapshots/{sid}/hash", json={"algo": "md5"}).status_code == 400


def test_api_hash_409_active_task(client, env) -> None:
    data_root, _, sid = env
    # 直接在 catalog.tasks 插一行活跃 hash 任务 → 提交去重应 409
    cat = sqlite3.connect(catalog_path(data_root))
    cat.execute(
        "INSERT INTO tasks(task_id, kind, payload_json, status, created_at)"
        " VALUES('task_fake', 'hash', ?, 'pending', '2026-01-01T00:00:00Z')",
        (f'{{"snapshot_id": "{sid}"}}',),
    )
    cat.commit()
    cat.close()
    r = client.post(f"/api/snapshots/{sid}/hash", json={})
    assert r.status_code == 409


# ---------------------------------------------------------------- CLI


def test_cli_hash_smoke(env, capsys) -> None:
    from cold_manifest.cli import main

    data_root, _, sid = env
    rc = main(["hash", sid, "--data-root", str(data_root)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "计算：2" in out

    # 已全部有哈希 → 跳过提示
    rc = main(["hash", sid, "--data-root", str(data_root)])
    assert rc == 0
    assert "无需补算" in capsys.readouterr().out


def test_cli_hash_unknown_snapshot(env, capsys) -> None:
    from cold_manifest.cli import main

    data_root, _, _ = env
    rc = main(["hash", "nope/123", "--data-root", str(data_root)])
    assert rc == 2


# ---------------------------------------------------------------- host_path 语义修复

def _fake_probe():
    from cold_manifest.probe import DiskInfo, VolumeInfo
    vol = VolumeInfo(
        filesystem="ext4", label="FAKELBL", volume_serial_hex="abcd-1234",
        partition_uuid="1111-2222", partition_index=1, partition_table_type="GPT",
        capacity_bytes=10_000_000_000, free_bytes=9_000_000_000,
        mount_point="/mnt/fake", device_path="/dev/sdb1",
    )
    disk = DiskInfo(
        physical_model="Fake Disk 5000", physical_serial="SERFAKE123",
        disk_serial="SERFAKE123", serial_source="probe",
        bridge_model="", interface_type="USB", capacity_bytes=20_000_000_000,
        firmware="fw1", smart_status="unavailable",
    )

    def probe(path, *, manual_serial=None, smartctl=True):
        return vol, disk

    return probe


def test_collect_host_path_is_source_dir_and_hash_works(tmp_path, monkeypatch, capsys) -> None:
    """回归：采集快照的 host_path 必须是被扫描根目录，cldm hash 直接可用。

    修复前 host_path 存 snapshot.db 路径 → hash 必报"源目录不可用"。
    """
    from cold_manifest.catalog import connect_catalog
    from cold_manifest.cli import main
    from cold_manifest.collect import collect_volume

    monkeypatch.setattr("cold_manifest.collect.probe_path", _fake_probe())
    scan_root = tmp_path / "vol"
    scan_root.mkdir()
    payload = {"f1.txt": b"alpha\n", "sub/f2.bin": bytes(range(256))}
    for rel, data in payload.items():
        p = scan_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    data_root = tmp_path / "data"

    result = collect_volume(scan_root, data_root=data_root, on_disk_copy=False)
    sid = result.snapshot_id

    # host_path 是目录（绝对路径 = 被扫描根）
    cat = connect_catalog(data_root)
    host_path = Path(cat.execute(
        "SELECT host_path FROM snapshots WHERE snapshot_id=?", (sid,)).fetchone()[0])
    cat.close()
    assert host_path.is_dir()
    assert host_path == scan_root.resolve()

    # 快照库 meta 也写了 root_path
    conn = _open_rw(_snap_db(data_root, sid))
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    assert Path(meta["root_path"]) == scan_root.resolve()
    conn.close()

    # cldm hash 全链路成功且与 hashlib 一致
    rc = main(["hash", sid, "--data-root", str(data_root)])
    assert rc == 0
    assert "错误" not in capsys.readouterr().err
    conn = _open_rw(_snap_db(data_root, sid))
    rows = {r["path"]: r["hash_hex"] for r in conn.execute(
        "SELECT path, hash_hex FROM entries WHERE type='file'")}
    conn.close()
    assert rows["f1.txt"] == hashlib.sha256(payload["f1.txt"]).hexdigest()
    assert rows["sub/f2.bin"] == hashlib.sha256(payload["sub/f2.bin"]).hexdigest()


def _register_dirty_snapshot(data_root: Path, host: Path, meta_root: "Path | None") -> str:
    """脏数据快照：host_path 指向 snapshot.db 文件（旧版采集的写法）。"""
    sid = "dirtvol/20260201T000000Z"
    db_dir = data_root / "dirtvol" / "20260201T000000Z"
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_dir / "snapshot.db")
    init_snapshot(conn)
    conn.executemany(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type, size_bytes,"
        " mtime_ns, path_norm) VALUES(?,?,?,?,?,?,?,?,?)",
        _std_rows(),
    )
    meta = {"status": "sealed"}
    if meta_root is not None:
        meta["root_path"] = str(meta_root)
    conn.executemany("INSERT INTO meta(key, value) VALUES(?,?)", list(meta.items()))
    conn.commit()
    conn.close()
    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    ensure_disk(cat, "DISK1", physical_model="TEST")
    ensure_volume(cat, "dirtvol", "DISK1")
    register_snapshot(cat, sid, "dirtvol", status="sealed",
                      host_path=str(db_dir / "snapshot.db"))
    cat.commit()
    cat.close()
    return sid


def test_hash_dirty_host_path_falls_back_to_meta(env) -> None:
    """host_path=snapshot.db 时回退 meta.root_path；--root 显式覆盖优先。"""
    data_root, host, _ = env
    sid = _register_dirty_snapshot(data_root, host, meta_root=host)
    conn = _open_rw(_snap_db(data_root, sid))
    r = hash_snapshot(conn, data_root, sid)
    assert r["computed"] == 2 and r["host_path"] == str(host)
    conn.close()


def test_hash_dirty_host_path_explicit_root_overrides(env) -> None:
    data_root, host, _ = env
    sid = _register_dirty_snapshot(data_root, host, meta_root=None)
    # meta 也没有 → 显式 --root 成功
    conn = _open_rw(_snap_db(data_root, sid))
    r = hash_snapshot(conn, data_root, sid, root=host)
    assert r["computed"] == 2
    # 显式 root 不是目录 → 报错
    with pytest.raises(HashError, match="不是目录"):
        hash_snapshot(conn, data_root, sid, root=host / "nope")
    conn.close()


def test_hash_dirty_host_path_no_fallback_error_has_hint(env) -> None:
    """无任何可用源目录：报错信息提示 --root。"""
    data_root, host, _ = env
    sid = _register_dirty_snapshot(data_root, host, meta_root=None)
    conn = _open_rw(_snap_db(data_root, sid))
    with pytest.raises(HashError, match="--root"):
        hash_snapshot(conn, data_root, sid)
    conn.close()


def test_cli_hash_dirty_snapshot_root_flag(env, capsys) -> None:
    from cold_manifest.cli import main

    data_root, host, _ = env
    sid = _register_dirty_snapshot(data_root, host, meta_root=None)
    # 不带 --root：退出码 2，错误含提示
    rc = main(["hash", sid, "--data-root", str(data_root)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "不可用" in err and "--root" in err
    # 带 --root：成功
    rc = main(["hash", sid, "--data-root", str(data_root), "--root", str(host)])
    assert rc == 0
    assert "计算：2" in capsys.readouterr().out


def test_api_hash_with_and_without_root(client, env) -> None:
    data_root, host, _ = env
    sid = _register_dirty_snapshot(data_root, host, meta_root=None)
    # 不带 root → 任务 error，错误含提示
    r = client.post(f"/api/snapshots/{sid}/hash", json={})
    assert r.status_code == 201
    task = _wait_task(client, r.json()["task_id"])
    assert task["status"] == "error"
    assert "--root" in (task.get("error") or "")
    # 带 root → done
    r = client.post(f"/api/snapshots/{sid}/hash", json={"root": str(host)})
    assert r.status_code == 201
    task = _wait_task(client, r.json()["task_id"])
    assert task["status"] == "done"
    assert task["result"]["computed"] == 2
    assert task["payload"]["root"] == str(host)


def test_api_hash_unmounted_source_clear_error(client, env) -> None:
    data_root, _, sid = env
    cat = sqlite3.connect(catalog_path(data_root))
    cat.execute("UPDATE snapshots SET host_path='/mnt/__unmounted__'")
    cat.commit()
    cat.close()
    r = client.post(f"/api/snapshots/{sid}/hash", json={})
    assert r.status_code == 201
    task = _wait_task(client, r.json()["task_id"])
    assert task["status"] == "error"
    assert "不可用" in (task.get("error") or "")
