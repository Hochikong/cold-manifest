"""verify.py：盘上副本与源文件完整性校验测试。"""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from cold_manifest.catalog import catalog_path, ensure_disk, ensure_volume, \
    register_snapshot
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.verify import VerifyError, verify_snapshot_copy

A_TXT = b"hello cold manifest\n"
B_BIN = bytes(range(256)) * 40  # 10240 B


# ---------------------------------------------------------------- fixtures

def _build_snapshot(data_root: Path, host: Path, sid_ts: str,
                    rows: "list[tuple]", meta: "dict[str, str]") -> str:
    """建封库快照（entries + meta 自定）并注册 catalog，返回 snapshot_id。"""
    db_dir = data_root / sid_ts.split("/")[0] / sid_ts.split("/")[1]
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_dir / "snapshot.db")
    init_snapshot(conn)
    conn.executemany(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type,"
        " size_bytes, mtime_ns, path_norm) VALUES(?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO meta(key, value) VALUES(?,?)",
                     list(meta.items()))
    conn.commit()
    conn.close()

    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    ensure_disk(cat, "DISK1", physical_model="TEST")
    ensure_volume(cat, sid_ts.split("/")[0], "DISK1")
    register_snapshot(cat, sid_ts, sid_ts.split("/")[0], status="sealed",
                      host_path=str(host))
    cat.commit()
    cat.close()
    return sid_ts


def _std_rows(hashes: "dict[str, str]") -> "list[tuple]":
    """root + docs/{a.txt,b.bin,c.txt}，哈希由 hashes[path] 提供。"""
    return [
        (1, 0, ".", "", 0, "dir", None, None, None),
        (2, 1, "docs", "docs", 1, "dir", None, None, None),
        (3, 2, "docs/a.txt", "a.txt", 2, "file", len(A_TXT), 1111, "docs/a.txt"),
        (4, 2, "docs/b.bin", "b.bin", 2, "file", len(B_BIN), 2222, "docs/b.bin"),
        (5, 2, "docs/c.txt", "c.txt", 2, "file", len(A_TXT), 3333, "docs/c.txt"),
    ]


def _hash_rows_meta(host: Path, policy: str = "full") -> "dict[str, str]":
    """标准 meta：hash_policy + 每个文件条目的哈希写进 meta 供测试参考。"""
    meta = {"status": "sealed", "scan_root": str(host), "root_path": str(host),
            "hash_policy": policy}
    for name, content in (("a.txt", A_TXT), ("c.txt", A_TXT), ("b.bin", B_BIN)):
        meta[f"hash:docs/{name}"] = hashlib.sha256(content).hexdigest()
    return meta


@pytest.fixture()
def env(tmp_path: Path):
    """data_root + 源目录（3 个真实文件）+ 盘上副本（主机库的逐字节拷贝 + 旁车）。"""
    data_root = tmp_path / "data"
    host = tmp_path / "host"
    (host / "docs").mkdir(parents=True)
    (host / "docs" / "a.txt").write_bytes(A_TXT)
    (host / "docs" / "b.bin").write_bytes(B_BIN)
    (host / "docs" / "c.txt").write_bytes(A_TXT)
    for entry_id, mtime in ((3, 1111), (4, 2222), (5, 3333)):
        name = {3: "a.txt", 4: "b.bin", 5: "c.txt"}[entry_id]
        os.utime(host / "docs" / name, ns=(mtime, mtime))

    sid = _build_snapshot(data_root, host, "vol/20260101T000000Z",
                          _std_rows(None), _hash_rows_meta(host))
    # 给快照库 entries 写 hash_hex/hash_state（模拟 cldm hash 完成；真实枚举 full/sampled/error）
    db = data_root / "vol" / "20260101T000000Z" / "snapshot.db"
    conn = sqlite3.connect(str(db))
    for entry_id, name in ((3, "a.txt"), (4, "b.bin"), (5, "c.txt")):
        conn.execute(
            "UPDATE entries SET hash_algo='sha256', hash_hex=?, hash_state='full'"
            " WHERE entry_id=?",
            (hashlib.sha256(A_TXT if name != "b.bin" else B_BIN).hexdigest(),
             entry_id))
    conn.commit()
    conn.close()

    # 盘上副本：目录 + 逐字节拷贝 + 旁车 snapshot.json + on_disk_copies 记录
    copy_dir = host / "_coldmanifest" / "vol" / "20260101T000000Z"
    copy_dir.mkdir(parents=True)
    (copy_dir / "snapshot.db").write_bytes(db.read_bytes())
    recorded_sha = hashlib.sha256(db.read_bytes()).hexdigest()
    (copy_dir / "snapshot.json").write_text(json.dumps({
        "volume_id": "vol",
        "collect_time": "2026-01-01T00:00:00Z",
        "files": 3,
        "total_bytes": len(A_TXT) + len(B_BIN) + len(A_TXT),
    }), encoding="utf-8")
    cat = sqlite3.connect(catalog_path(data_root))
    cat.execute("INSERT INTO on_disk_copies(snapshot_id, disk_path, sha256, status)"
                " VALUES(?,?,?,'ok')",
                (sid, str(copy_dir / "snapshot.db"), recorded_sha))
    cat.commit()
    cat.close()
    return data_root, host, sid


def _snap_db(data_root: Path, sid: str) -> Path:
    return data_root / sid.split("/")[0] / sid.split("/")[1] / "snapshot.db"


# ---------------------------------------------------------------- 副本对账


def test_copy_all_ok(env) -> None:
    data_root, host, sid = env
    r = verify_snapshot_copy(data_root, sid)
    assert r["ok"] is True
    assert r["copy"]["status"] == "ok"
    assert r["copy"]["disk_sha256"] == r["copy"]["recorded_sha256"]
    assert r["copy"]["host_status"] == "unchanged"
    assert r["sidecar"]["status"] == "ok"
    assert r["sidecar"]["problems"] == []
    assert r["source"]["checked"] == 0  # 未要求抽检


def test_copy_ok_after_hash_modifies_host_db(env) -> None:
    """场景①：collect → cldm hash（改主机库）→ verify 仍 ok（副本对账看记录值）。"""
    data_root, host, sid = env
    db = _snap_db(data_root, sid)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE entries SET mtime_ns=9999 WHERE entry_id=3")
    conn.commit()
    conn.close()
    r = verify_snapshot_copy(data_root, sid)
    assert r["copy"]["status"] == "ok"
    assert r["copy"]["host_status"] == "modified_since_collection"
    assert r["ok"] is True


def test_host_db_modified_is_info_only(env) -> None:
    """场景④：主机库被后续操作改写（如 hash/FTS）→ host_status=modified，不算失败。"""
    data_root, host, sid = env
    db = _snap_db(data_root, sid)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE entries SET mtime_ns=4242 WHERE entry_id=3")
    conn.commit()
    conn.close()
    r = verify_snapshot_copy(data_root, sid)
    assert r["copy"]["host_status"] == "modified_since_collection"
    assert r["copy"]["status"] == "ok"
    assert r["ok"] is True


def test_disk_copy_no_record_not_recorded(env) -> None:
    """场景③：无 on_disk_copies 行（import-legacy 等）→ not_recorded，不误报。"""
    data_root, host, sid = env
    cat = sqlite3.connect(catalog_path(data_root))
    cat.execute("DELETE FROM on_disk_copies WHERE snapshot_id=?", (sid,))
    cat.commit()
    cat.close()
    r = verify_snapshot_copy(data_root, sid)
    assert r["copy"]["status"] == "not_recorded"
    assert r["copy"]["recorded_sha256"] is None
    assert r["ok"] is True  # 无记录不算失败


def test_disk_copy_one_byte_changed(env) -> None:
    data_root, host, sid = env
    disk = host / "_coldmanifest" / "vol" / "20260101T000000Z" / "snapshot.db"
    raw = bytearray(disk.read_bytes())
    raw[-1] ^= 0xFF
    disk.write_bytes(bytes(raw))
    r = verify_snapshot_copy(data_root, sid)
    assert r["copy"]["status"] == "mismatch"
    assert r["copy"]["disk_sha256"] != r["copy"]["recorded_sha256"]
    assert r["ok"] is False


def test_disk_copy_size_differs(env) -> None:
    data_root, host, sid = env
    disk = host / "_coldmanifest" / "vol" / "20260101T000000Z" / "snapshot.db"
    disk.write_bytes(disk.read_bytes() + b"x")
    r = verify_snapshot_copy(data_root, sid)
    assert r["copy"]["status"] == "mismatch"
    assert r["ok"] is False


def test_disk_copy_missing(env) -> None:
    data_root, host, sid = env
    (host / "_coldmanifest" / "vol" / "20260101T000000Z" / "snapshot.db").unlink()
    r = verify_snapshot_copy(data_root, sid)
    assert r["copy"]["status"] == "missing_disk"
    assert r["copy"]["disk_sha256"] is None
    assert r["ok"] is False


def test_sidecar_field_mismatch_recorded(env) -> None:
    data_root, host, sid = env
    sc = host / "_coldmanifest" / "vol" / "20260101T000000Z" / "snapshot.json"
    data = json.loads(sc.read_text(encoding="utf-8"))
    data["files"] = 999
    data["total_bytes"] = 1
    sc.write_text(json.dumps(data), encoding="utf-8")
    r = verify_snapshot_copy(data_root, sid)
    assert r["copy"]["status"] == "ok"
    assert r["sidecar"]["status"] == "problems"
    assert len(r["sidecar"]["problems"]) == 2
    assert r["ok"] is False


def test_sidecar_missing(env) -> None:
    data_root, host, sid = env
    (host / "_coldmanifest" / "vol" / "20260101T000000Z" / "snapshot.json").unlink()
    r = verify_snapshot_copy(data_root, sid)
    assert r["sidecar"]["status"] == "missing"
    assert r["ok"] is False


# ---------------------------------------------------------------- 坏输入


def test_unregistered_snapshot(tmp_path) -> None:
    with pytest.raises(VerifyError, match="未注册"):
        verify_snapshot_copy(tmp_path, "nope/20260101T000000Z")


def test_unsealed_snapshot(env) -> None:
    data_root, host, sid = env
    db = _snap_db(data_root, sid)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE meta SET value='collecting' WHERE key='status'")
    conn.commit()
    conn.close()
    with pytest.raises(VerifyError, match="未封库"):
        verify_snapshot_copy(data_root, sid)


def test_host_db_missing(env) -> None:
    data_root, host, sid = env
    _snap_db(data_root, sid).unlink()
    with pytest.raises(VerifyError, match="缺失"):
        verify_snapshot_copy(data_root, sid)


def test_scan_root_unresolvable(tmp_path) -> None:
    """meta scan_root 与 host_path 都不可用 → VerifyError 提示 --root。"""
    data_root = tmp_path / "data"
    host = tmp_path / "gone"  # 不存在的扫描根
    sid = _build_snapshot(data_root, host, "vol/20260101T000000Z",
                          _std_rows(None), {"status": "sealed",
                                            "scan_root": str(host)})
    with pytest.raises(VerifyError, match="--root"):
        verify_snapshot_copy(data_root, sid)


def test_root_override(env) -> None:
    """root_override 指向别处的同名副本结构也能对上（副本与源目录解耦）。"""
    data_root, host, sid = env
    other = host.parent / "other_root"
    (other / "_coldmanifest" / "vol" / "20260101T000000Z").mkdir(parents=True)
    import shutil
    shutil.copytree(host / "_coldmanifest" / "vol" / "20260101T000000Z",
                    other / "_coldmanifest" / "vol" / "20260101T000000Z",
                    dirs_exist_ok=True)
    r = verify_snapshot_copy(data_root, sid, root_override=other)
    assert r["copy"]["status"] == "ok"
    assert r["disk_db"] == str(other / "_coldmanifest" / "vol"
                               / "20260101T000000Z" / "snapshot.db")


def test_root_override_not_a_dir(env) -> None:
    data_root, host, sid = env
    with pytest.raises(VerifyError, match="不是目录"):
        verify_snapshot_copy(data_root, sid, root_override=env[1] / "docs" / "a.txt")


# ---------------------------------------------------------------- 源文件抽检


def test_source_sample_all_match(env) -> None:
    data_root, host, sid = env
    r = verify_snapshot_copy(data_root, sid, sample=3)
    assert r["source"]["checked"] == 3
    assert r["source"]["match"] == 3
    assert r["source"]["mismatch"] == 0
    assert r["ok"] is True
    for s in r["source"]["samples"]:
        assert s["status"] == "match"


def test_source_tampered_file(env) -> None:
    data_root, host, sid = env
    (host / "docs" / "a.txt").write_bytes(b"tampered!\n")
    r = verify_snapshot_copy(data_root, sid, sample=3)
    assert r["source"]["mismatch"] == 1
    bad = [s for s in r["source"]["samples"] if s["status"] == "mismatch"]
    assert len(bad) == 1 and bad[0]["path"] == "docs/a.txt"
    assert bad[0]["actual"] is not None and bad[0]["actual"] != bad[0]["expected"]
    assert r["ok"] is False


def test_source_deleted_file(env) -> None:
    data_root, host, sid = env
    (host / "docs" / "c.txt").unlink()
    r = verify_snapshot_copy(data_root, sid, sample=3)
    assert r["source"]["missing"] == 1
    bad = [s for s in r["source"]["samples"] if s["status"] == "missing"]
    assert bad[0]["path"] == "docs/c.txt" and bad[0]["actual"] is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root 不受 0o000 权限限制")
def test_source_unreadable_file(env) -> None:
    data_root, host, sid = env
    p = host / "docs" / "c.txt"
    p.chmod(0o000)
    try:
        r = verify_snapshot_copy(data_root, sid, sample=3)
    finally:
        p.chmod(0o644)
    assert r["source"]["unreadable"] == 1


def test_source_seed_reproducible(env) -> None:
    data_root, host, sid = env
    r1 = verify_snapshot_copy(data_root, sid, sample=2, seed=42)
    r2 = verify_snapshot_copy(data_root, sid, sample=2, seed=42)
    paths = lambda r: [s["path"] for s in r["source"]["samples"]]
    assert paths(r1) == paths(r2)
    assert len(paths(r1)) == 2


def test_sampled_entries_excluded(env) -> None:
    """hash_state='sampled' 的条目不参与抽检（指纹不能证明内容等值）。"""
    data_root, host, sid = env
    conn = sqlite3.connect(str(_snap_db(data_root, sid)))
    conn.execute("UPDATE entries SET hash_state='sampled' WHERE entry_id=4")
    conn.commit()
    conn.close()
    r = verify_snapshot_copy(data_root, sid, sample=3)
    assert r["source"]["checked"] == 2  # 只有 2 条 full 可抽
    assert all(s["path"] != "docs/b.bin" for s in r["source"]["samples"])


def test_source_sampled_policy_unavailable(env) -> None:
    data_root, host, sid = env
    conn = sqlite3.connect(str(_snap_db(data_root, sid)))
    conn.execute("UPDATE meta SET value='sampled' WHERE key='hash_policy'")
    conn.commit()
    conn.close()
    # 主机库被 hash 改动不影响副本对账：对账基准是采集时记录值（catalog），盘上副本不动
    r = verify_snapshot_copy(data_root, sid, sample=3)
    assert r["source"]["available"] is False
    assert "sampled" in r["source"]["error"]
    assert r["source"]["checked"] == 0
    assert r["copy"]["status"] == "ok"  # 副本对账不受影响


def test_source_full_and_progress(env) -> None:
    data_root, host, sid = env
    calls: "list[tuple[int, int]]" = []
    r = verify_snapshot_copy(data_root, sid, full=True,
                             progress_cb=lambda done, total: calls.append((done, total)))
    assert r["source"]["checked"] == 3
    assert r["source"]["match"] == 3
    assert calls == [(1, 3), (2, 3), (3, 3)]
    assert r["ok"] is True


def test_sample_larger_than_pool(env) -> None:
    data_root, host, sid = env
    r = verify_snapshot_copy(data_root, sid, sample=100)
    assert r["source"]["checked"] == 3


def test_negative_sample_rejected(env) -> None:
    data_root, host, sid = env
    with pytest.raises(VerifyError, match="sample"):
        verify_snapshot_copy(data_root, sid, sample=-1)


# ---------------------------------------------------------------- CLI 接线

def _run_cli(argv: "list[str]") -> int:
    from cold_manifest.cli import main
    return main(argv)


def test_cli_verify_copy_ok(env, capsys):
    """接线冒烟：正常副本 → 退出码 0，输出含副本结论与抽检统计。"""
    data_root, host, sid = env
    rc = _run_cli(["verify-copy", sid, "--data-root", str(data_root),
                   "--sample", "3", "--seed", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "校验：" in out
    assert "副本与采集记录一致" in out
    assert "match=3" in out


def test_cli_verify_copy_mismatch_exit1(env, capsys):
    """盘上副本被篡改 → 退出码 1，输出提示不一致。"""
    data_root, host, sid = env
    copy_db = (host / "_coldmanifest" / sid.split("/")[0]
               / sid.split("/")[1] / "snapshot.db")
    raw = bytearray(copy_db.read_bytes())
    raw[-1] ^= 0xFF
    copy_db.write_bytes(bytes(raw))

    rc = _run_cli(["verify-copy", sid, "--data-root", str(data_root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "发现不一致" in out
    assert "不一致" in out


def test_cli_verify_copy_missing_snapshot_exit2(tmp_path, capsys):
    """快照不存在 → 退出码 2 且错误走 stderr。"""
    rc = _run_cli(["verify-copy", "nope/20260101T000000Z",
                   "--data-root", str(tmp_path / "empty")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "错误：" in err
