"""DELETE /api/tasks/{task_id} 测试：终态可删、pending/running 409、未知 404、
批次子任务删除后批次仍可访问、只删任务行不动物理快照目录。"""

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.server import create_app
from test_collect import _fake_probe


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(str(tmp_path / "dataroot"))
    with TestClient(app) as c:  # with 触发 lifespan（TaskRunner.start）
        yield c, tmp_path / "dataroot"


def _make_tree(root: Path, dirs: int = 2) -> None:
    for i in range(dirs):
        d = root / f"d{i}"
        d.mkdir(parents=True)
        (d / "a.txt").write_bytes(b"x" * 100)


def _wait_task(c: TestClient, task_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = c.get(f"/api/tasks/{task_id}").json()
        if body["status"] in ("done", "error", "cancelled"):
            return body
        time.sleep(0.05)
    raise TimeoutError(f"任务未在 {timeout}s 内到终态：{task_id}")


def _collect_done(c: TestClient, tmp_path: Path, monkeypatch, name: str = "vol") -> dict:
    """跑一个到 done 的采集任务，返回任务 dict（含 result.snapshot_id）。"""
    monkeypatch.setattr("cold_manifest.collect.probe_path", _fake_probe())
    scan_root = tmp_path / name
    _make_tree(scan_root)
    r = c.post("/api/collect", json={"path": str(scan_root)})
    assert r.status_code == 201, r.text
    task = _wait_task(c, r.json()["task_id"])
    assert task["status"] == "done", task
    return task


def test_delete_done_task(client, tmp_path: Path, monkeypatch) -> None:
    c, _ = client
    task = _collect_done(c, tmp_path, monkeypatch)
    tid = task["id"]

    r = c.delete(f"/api/tasks/{tid}")
    assert r.status_code == 200
    assert r.json() == {"deleted": True, "task_id": tid}
    # 行真的没了
    assert c.get(f"/api/tasks/{tid}").status_code == 404
    # 再删 → 404
    assert c.delete(f"/api/tasks/{tid}").status_code == 404


def test_delete_running_and_pending_409(client, tmp_path: Path, monkeypatch) -> None:
    c, _ = client
    gate = threading.Event()

    def slow_probe(path, *, manual_serial=None, smartctl=True):
        gate.wait(10)
        return _fake_probe()(path, manual_serial=manual_serial, smartctl=smartctl)

    monkeypatch.setattr("cold_manifest.collect.probe_path", slow_probe)
    root1 = tmp_path / "vol1"
    _make_tree(root1)
    r1 = c.post("/api/collect", json={"path": str(root1)})
    assert r1.status_code == 201
    running_id = r1.json()["task_id"]
    # 等 running（probe 已进入 gate）
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if c.get(f"/api/tasks/{running_id}").json()["status"] == "running":
            break
        time.sleep(0.02)

    # 第二个任务（不同路径）排在后面 → pending
    root2 = tmp_path / "vol2"
    _make_tree(root2)
    r2 = c.post("/api/collect", json={"path": str(root2), "volume_id": "SEROTHER_P1"})
    assert r2.status_code == 201
    pending_id = r2.json()["task_id"]
    assert c.get(f"/api/tasks/{pending_id}").json()["status"] == "pending"

    # running → 409，行还在
    r = c.delete(f"/api/tasks/{running_id}")
    assert r.status_code == 409
    assert c.get(f"/api/tasks/{running_id}").status_code == 200
    # pending → 409，行还在
    r = c.delete(f"/api/tasks/{pending_id}")
    assert r.status_code == 409
    assert "取消" in r.json()["detail"]
    assert c.get(f"/api/tasks/{pending_id}").status_code == 200

    gate.set()
    _wait_task(c, running_id)
    _wait_task(c, pending_id)


def test_delete_unknown_task_404(client) -> None:
    c, _ = client
    r = c.delete("/api/tasks/task_nope")
    assert r.status_code == 404
    assert "任务不存在" in r.json()["detail"]


def test_delete_batch_child_batch_still_listed(client, tmp_path: Path, monkeypatch) -> None:
    """删除批次子任务后 GET /api/batches/{id} 仍可访问，计数相应减少。"""
    from test_collect_batch import _fake_probe_per_path, _make_tree as _mt, _targets_for

    c, _ = client
    vols = [tmp_path / f"bvol{i}" for i in range(2)]
    for v in vols:
        _mt(v)
    monkeypatch.setattr("cold_manifest.api.routes_collect.enumerate_disk_volumes",
                        lambda p: (_targets_for([str(v) for v in vols]), []))
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _fake_probe_per_path([str(v) for v in vols]))
    r = c.post("/api/collect", json={"path": str(vols[0]), "all_partitions": True})
    assert r.status_code == 201, r.text
    batch_id = r.json()["batch_id"]
    task_ids = r.json()["task_ids"]
    for tid in task_ids:
        assert _wait_task(c, tid)["status"] == "done"

    # 删除一个子任务
    assert c.delete(f"/api/tasks/{task_ids[0]}").status_code == 200
    batch = c.get(f"/api/batches/{batch_id}")
    assert batch.status_code == 200
    body = batch.json()
    assert len(body["tasks"]) == 1
    assert body["tasks"][0]["id"] == task_ids[1]
    assert body["summary"] == {"done": 1}


def test_delete_task_keeps_snapshot_dir(client, tmp_path: Path, monkeypatch) -> None:
    """只删任务行：result.snapshot_id 指向的物理快照目录仍在。"""
    c, data_root = client
    task = _collect_done(c, tmp_path, monkeypatch)
    tid = task["id"]
    snapshot_id = task["result"]["snapshot_id"]  # 形如 VOL/ts
    snap_dir = data_root / snapshot_id
    assert snap_dir.is_dir()

    assert c.delete(f"/api/tasks/{tid}").status_code == 200
    assert snap_dir.is_dir()
    assert (snap_dir / "snapshot.db").exists()
