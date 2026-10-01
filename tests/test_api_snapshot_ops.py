"""快照域操作端点测试（P4.5-fix-59）：verify-copy / build-fts / build-stats /
report sections / PATCH pinned+notes。"""

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import catalog_path, ensure_disk, ensure_volume, register_snapshot
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.server import create_app

SID = "vol/20260101T000000Z"
BASE = "/api/snapshots"

# 三个文件的 path 与 sha256（sha256sum 预计算，供源文件抽检比对）
FILES = {
    "docs/a.txt": b"alpha content\n",
    "docs/b.txt": b"beta content\n",
    "big.bin": b"big" * 1000,
}


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _build_snapshot_db(path: Path, scan_root: Path) -> None:
    """合成已封库、hash_policy=full 的小快照库，meta.scan_root 指向 scan_root。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    init_snapshot(conn)
    # 目录行：root('.') + 按路径推导的中间目录
    dir_rows = [(1, 0, ".", "", 0, "dir", None, None, "")]
    dir_ids = {}
    eid = 1
    for rel in FILES:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            dirpath = "/".join(parts[:i])
            if dirpath not in dir_ids:
                eid += 1
                dir_ids[dirpath] = eid
                parent = dir_ids.get("/".join(parts[:i - 1]), 1)
                dir_rows.append((eid, parent, dirpath, parts[i - 1], i, "dir", None, None, ""))
    conn.executemany(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type,"
        " size_bytes, mtime_ns, ext) VALUES(?,?,?,?,?,?,?,?,?)", dir_rows)
    # 文件行（带 full 哈希）
    for rel, data in FILES.items():
        parts = rel.split("/")
        parent = dir_ids.get("/".join(parts[:-1]), 1)
        eid += 1
        conn.execute(
            "INSERT INTO entries(entry_id, parent_id, path, name, depth, type,"
            " size_bytes, mtime_ns, ext, hash_state, hash_hex) VALUES(?,?,?,?,?,?,?,?,?,'full',?)",
            (eid, parent, rel, parts[-1], len(parts), "file", len(data), 100,
             "." + rel.rsplit(".", 1)[1], _sha(data)))
    conn.executemany("INSERT INTO meta(key, value) VALUES(?,?)", [
        ("status", "sealed"),
        ("hash_policy", "full"),
        ("scan_root", str(scan_root)),
        ("source", "test"),
    ])
    conn.commit()
    conn.close()


def _make_scan_root(root: Path, with_copy: "Path | None" = None) -> Path:
    """扫描根：与快照库同内容的源文件 + 旁车 snapshot.json；（可选）带盘上副本。"""
    for rel, data in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    copy_dir = root / "_coldmanifest" / "vol" / "20260101T000000Z"
    copy_dir.mkdir(parents=True, exist_ok=True)
    (copy_dir / "snapshot.json").write_text(
        json.dumps({"volume_id": "vol", "collect_time": "2026-01-01T00:00:00Z",
                    "files": len(FILES),
                    "total_bytes": sum(len(d) for d in FILES.values())}),
        encoding="utf-8")
    if with_copy is not None:
        (copy_dir / "snapshot.db").write_bytes(with_copy.read_bytes())
    return root


@pytest.fixture()
def env(tmp_path: Path):
    """(client, data_root, scan_root)：已封库快照 + 扫描根 + 盘上副本对账记录。"""
    data_root = tmp_path / "data"
    scan_root = tmp_path / "src"
    host_dir = data_root / "vol" / "20260101T000000Z"
    _make_scan_root(scan_root, with_copy=None)
    _build_snapshot_db(host_dir / "snapshot.db", scan_root)

    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    ensure_disk(cat, "DISK1", physical_model="TOSHIBA", capacity_bytes=2000)
    ensure_volume(cat, "vol", "DISK1", filesystem="NTFS")
    register_snapshot(cat, SID, "vol", status="sealed", host_path=str(host_dir),
                      file_count=3, dir_count=2, total_bytes=3003, hash_policy="full",
                      zero_byte_count=0, max_depth=2, skipped_count=0)
    cat.commit()
    cat.close()
    return data_root, scan_root


@pytest.fixture()
def client(env) -> TestClient:
    with TestClient(create_app(data_root=str(env[0]))) as c:
        yield c


def _wait_task(client: TestClient, task_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/tasks/{task_id}").json()
        if body["status"] in ("done", "error", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"任务超时未完成：{task_id}")


# ---------------------------------------------------------------- verify-copy


def test_verify_copy_ok(client: TestClient) -> None:
    r = client.post(f"{BASE}/{SID}/verify-copy", json={"scope": "sample", "sample_size": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["snapshot_id"] == SID
    assert body["copy"]["status"] == "not_recorded"  # 无 on_disk_copies 记录
    assert body["sidecar"]["status"] in ("ok", "missing", "problems")
    assert body["source"]["available"] is True  # hash_policy=full，抽检可执行
    assert body["source"]["checked"] == 3
    assert body["source"]["mismatch"] == 0
    assert body["ok"] is True


def test_verify_copy_404_and_400(client: TestClient) -> None:
    assert client.post(f"{BASE}/nope/123/verify-copy", json={}).status_code == 404
    assert client.post(f"{BASE}/{SID}/verify-copy",
                       json={"scope": "bogus"}).status_code == 400
    assert client.post(f"{BASE}/{SID}/verify-copy",
                       json={"sample_size": 0}).status_code == 400
    assert client.post(f"{BASE}/{SID}/verify-copy",
                       json={"sample_size": 5001}).status_code == 400


def test_verify_copy_tampered_disk_copy(env: Path, client: TestClient) -> None:
    data_root, scan_root = env
    host_db = data_root / "vol" / "20260101T000000Z" / "snapshot.db"
    recorded = hashlib.sha256(host_db.read_bytes()).hexdigest()
    copy_dir = scan_root / "_coldmanifest" / "vol" / "20260101T000000Z"
    copy_dir.mkdir(parents=True, exist_ok=True)
    copy = copy_dir / "snapshot.db"
    copy.write_bytes(host_db.read_bytes())
    cat = sqlite3.connect(catalog_path(data_root))
    cat.execute("INSERT INTO on_disk_copies(snapshot_id, disk_path, sha256) VALUES(?,?,?)",
                (SID, str(copy_dir), recorded))
    cat.commit()
    cat.close()

    # 篡改前：ok（副本一致）
    r = client.post(f"{BASE}/{SID}/verify-copy", json={"scope": "sample", "sample_size": 1})
    assert r.status_code == 200 and r.json()["copy"]["status"] == "ok"

    # 篡改副本 → mismatch，整体 ok=False
    with open(copy, "ab") as f:
        f.write(b"tampered")
    r = client.post(f"{BASE}/{SID}/verify-copy", json={"scope": "sample", "sample_size": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["copy"]["status"] == "mismatch"
    assert body["ok"] is False


# ---------------------------------------------------------------- build-fts / build-stats


def test_build_fts_task(client: TestClient) -> None:
    r = client.post(f"{BASE}/{SID}/build-fts")
    assert r.status_code == 201, r.text
    task_id = r.json()["task_id"]
    assert r.json()["status"] == "pending"

    task = _wait_task(client, task_id)
    assert task["status"] == "done", task
    result = task["result"]
    assert result["built"] is True
    assert result["tokenizer"] in ("trigram", "unicode61")
    assert result["count"] == 5  # 3 文件 + root + docs 目录
    assert isinstance(result["seconds"], float)

    # 池内旧连接看不见新建的 FTS 表：先逐出再搜
    client.app.state.cldm.evict_snapshot(SID)
    r = client.get(f"{BASE}/{SID}/search", params={"q": "a.txt", "mode": "fulltext"})
    assert r.status_code == 200
    body = r.json()
    assert body["fulltext_available"] is True
    assert any(it["path"] == "docs/a.txt" for it in body["items"])


def test_build_fts_409_duplicate(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from cold_manifest import tasks as tasks_mod
    import threading

    gate = threading.Event()
    original = tasks_mod._FN_REGISTRY["build_fts"]

    def slow(payload, cb, cancel_event=None):  # 卡住任务使其保持 running
        gate.wait(timeout=10)
        return original(payload, cb, cancel_event)

    monkeypatch.setitem(tasks_mod._FN_REGISTRY, "build_fts", slow)
    try:
        first = client.post(f"{BASE}/{SID}/build-fts")
        assert first.status_code == 201
        # 等任务真正开始执行（running）再提交第二个
        tid = first.json()["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if client.get(f"/api/tasks/{tid}").json()["status"] == "running":
                break
            time.sleep(0.02)
        second = client.post(f"{BASE}/{SID}/build-fts")
        assert second.status_code == 409
    finally:
        gate.set()
    assert _wait_task(client, tid)["status"] == "done"


def test_build_stats_task(client: TestClient) -> None:
    r = client.post(f"{BASE}/{SID}/build-stats")
    assert r.status_code == 201, r.text
    task = _wait_task(client, r.json()["task_id"])
    assert task["status"] == "done", task
    result = task["result"]
    assert result["keys"] == 6
    assert isinstance(result["seconds"], float)

    client.app.state.cldm.evict_snapshot(SID)
    r = client.get(f"{BASE}/{SID}/stats")
    assert r.status_code == 200
    assert r.json()["precomputed"] is True


def test_build_endpoints_404_and_unsealed_400(env, tmp_path: Path) -> None:
    data_root, _ = env
    with TestClient(create_app(data_root=str(data_root))) as client:
        assert client.post(f"{BASE}/nope/123/build-fts").status_code == 404
        assert client.post(f"{BASE}/nope/123/build-stats").status_code == 404

        # 未封库：直接改库文件里的 meta，再逐出池内 immutable 连接
        db = data_root / "vol" / "20260101T000000Z" / "snapshot.db"
        conn = sqlite3.connect(db)
        conn.execute("UPDATE meta SET value='incomplete' WHERE key='status'")
        conn.commit()
        conn.close()
        client.app.state.cldm.evict_snapshot(SID)
        assert client.post(f"{BASE}/{SID}/build-fts").status_code == 400
        assert client.post(f"{BASE}/{SID}/build-stats").status_code == 400


# ---------------------------------------------------------------- report sections


def test_report_sections_filter(client: TestClient) -> None:
    r = client.get(f"{BASE}/{SID}/report")
    assert r.status_code == 200
    full = r.text
    assert "大小直方图" in full and "跳过项摘要" in full

    r = client.get(f"{BASE}/{SID}/report", params={"sections": "overview,sizes"})
    assert r.status_code == 200
    text = r.text
    assert "大小直方图" in text
    assert "扩展名 Top" not in text
    assert "跳过项摘要" not in text
    assert "顶层目录 Top" not in text

    r = client.get(f"{BASE}/{SID}/report", params={"sections": "extensions"})
    assert r.status_code == 200
    assert "扩展名 Top" in r.text


def test_report_sections_400(client: TestClient) -> None:
    r = client.get(f"{BASE}/{SID}/report", params={"sections": "overview,bogus"})
    assert r.status_code == 400
    assert "bogus" in r.json()["detail"]
    r = client.get(f"{BASE}/{SID}/report", params={"sections": ","})
    assert r.status_code == 400


# ---------------------------------------------------------------- PATCH pinned / notes


def test_patch_snapshot(client: TestClient) -> None:
    r = client.patch(f"{BASE}/{SID}", json={"pinned": True, "notes": "重要盘"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pinned"] == 1
    assert body["notes"] == "重要盘"
    # 持久化
    assert client.get(f"{BASE}/{SID}").json()["pinned"] == 1
    # 幂等重复更新
    r = client.patch(f"{BASE}/{SID}", json={"notes": "x"})
    assert r.status_code == 200 and r.json()["pinned"] == 1 and r.json()["notes"] == "x"


def test_patch_snapshot_400_404(client: TestClient) -> None:
    assert client.patch(f"{BASE}/{SID}", json={}).status_code == 400
    assert client.patch(f"{BASE}/{SID}",
                        json={"notes": "长" * 2001}).status_code == 400
    assert client.patch(f"{BASE}/nope/123", json={"pinned": True}).status_code == 404
    # 非法 sid（volume 段含非法字符）→ 400
    assert client.patch(f"{BASE}/!bad/123", json={"pinned": True}).status_code == 400
