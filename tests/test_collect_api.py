"""P1.1b 采集入 Web 测试：POST /api/collect、取消、SSE 进度流、孤儿清扫、400/404/409。"""

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.collect import CollectCancelled, collect_volume
from cold_manifest.probe import DiskInfo, VolumeInfo
from cold_manifest.server import create_app
from cold_manifest.tasks import _FN_REGISTRY
from test_collect import _fake_probe


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(str(tmp_path / "dataroot"))
    with TestClient(app) as c:  # with 触发 lifespan（TaskRunner.start + 孤儿清扫）
        yield c, tmp_path


def _make_tree(root: Path, dirs: int = 5) -> None:
    for i in range(dirs):
        d = root / f"d{i}"
        d.mkdir(parents=True)
        (d / "a.txt").write_bytes(b"x" * 100)
    (root / "top.bin").write_bytes(b"y" * 200)


def _wait_task(c: TestClient, task_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = c.get(f"/api/tasks/{task_id}").json()
        if body["status"] in ("done", "error", "cancelled"):
            return body
        time.sleep(0.05)
    raise TimeoutError(f"任务未在 {timeout}s 内到终态：{task_id}")


# ---------------------------------------------------------------- 基本采集流

def test_collect_api_e2e(client, tmp_path: Path, monkeypatch) -> None:
    c, _ = client
    monkeypatch.setattr("cold_manifest.collect.probe_path", _fake_probe())
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)

    r = c.post("/api/collect", json={"path": str(scan_root)})
    assert r.status_code == 201
    assert set(r.json()) == {"task_id", "status"}
    assert r.json()["status"] == "pending"

    task = _wait_task(c, r.json()["task_id"])
    assert task["status"] == "done"
    assert task["type"] == "collect"
    assert task["result"]["volume_id"] == "SERFAKE123_P1"
    assert task["result"]["files"] == 6
    # payload 记录全部采集参数
    assert task["payload"]["path"] == str(scan_root.resolve())
    assert task["payload"]["exclude_hidden"] is False
    assert task["payload"]["include_system"] is True
    assert task["payload"]["smartctl"] is True
    assert task["payload"]["on_disk_copy"] is True

    # 快照立即可见
    snaps = c.get("/api/snapshots").json()
    assert any(s["snapshot_id"].startswith("SERFAKE123_P1/") for s in snaps["items"])


def test_collect_api_progress_phases(client, tmp_path: Path, monkeypatch) -> None:
    """scan/copy/seal 细粒度进度：copy 按字节、seal 多步（total>3）。"""
    c, _ = client
    monkeypatch.setattr("cold_manifest.collect.probe_path", _fake_probe())
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)

    r = c.post("/api/collect", json={"path": str(scan_root)})
    task = _wait_task(c, r.json()["task_id"])
    assert task["status"] == "done"

    # 引擎层直接验证回调粒度（任务层有 200ms 节流，样本不全）
    phases: list[tuple[str, int, "int | None"]] = []
    collect_volume(scan_root, data_root=tmp_path / "dataroot2",
                   progress_cb=lambda ph, d, t: phases.append((ph, d, t)))
    copy_frames = [t for ph, d, t in phases if ph == "copy"]
    assert copy_frames and all(t and t > 0 for t in copy_frames)  # 按字节，total=库大小
    seal_totals = [t for ph, d, t in phases if ph == "seal"]
    assert max(seal_totals) > 3  # seal 拆 index/rollup(depth 多步)/optimize
    seen = []
    for ph, _, _ in phases:
        if not seen or seen[-1] != ph:
            seen.append(ph)
    assert seen == ["probe", "scan", "seal", "copy", "register", "done"]


def test_collect_api_400(client, tmp_path: Path) -> None:
    c, _ = client
    assert c.post("/api/collect", json={"path": str(tmp_path / "nope")}).status_code == 400
    f = tmp_path / "afile.txt"
    f.write_text("x")
    assert c.post("/api/collect", json={"path": str(f)}).status_code == 400
    assert c.post("/api/collect", json={"path": "relative/path"}).status_code == 400


def test_collect_api_409_same_path(client, tmp_path: Path, monkeypatch) -> None:
    """同 resolved path 已有活跃 collect 任务 → 409（dedupe 键 per-kind）。"""
    c, _ = client
    gate = threading.Event()

    def slow_probe(path, *, manual_serial=None, smartctl=True):
        gate.wait(10)
        return _fake_probe()(path, manual_serial=manual_serial, smartctl=smartctl)

    monkeypatch.setattr("cold_manifest.collect.probe_path", slow_probe)
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)

    r1 = c.post("/api/collect", json={"path": str(scan_root)})
    assert r1.status_code == 201
    # 等任务 running（probe 已进入 gate）
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if c.get(f"/api/tasks/{r1.json()['task_id']}").json()["status"] == "running":
            break
        time.sleep(0.02)
    # 同路径 → 409；不同路径（import 类的 dedupe 键不同）不受 collect 影响
    assert c.post("/api/collect", json={"path": str(scan_root)}).status_code == 409
    gate.set()
    assert _wait_task(c, r1.json()["task_id"])["status"] == "done"
    # 终态后活跃去重解除：换一个子目录路径再次提交可成功
    monkeypatch.setattr("cold_manifest.collect.probe_path", _fake_probe())
    r2 = c.post("/api/collect", json={"path": str(scan_root / "d0"),
                                      "volume_id": "SEROTHER_P1"})
    assert r2.status_code == 201
    assert _wait_task(c, r2.json()["task_id"])["status"] == "done"


# ---------------------------------------------------------------- 取消

def test_cancel_running_collect(client, tmp_path: Path, monkeypatch) -> None:
    """运行中取消 → cancelled，未封库目标目录被清理。"""
    c, _ = client
    gate = threading.Event()
    probe_started = threading.Event()

    def slow_probe(path, *, manual_serial=None, smartctl=True):
        probe_started.set()
        gate.wait(10)
        return _fake_probe()(path, manual_serial=manual_serial, smartctl=smartctl)

    monkeypatch.setattr("cold_manifest.collect.probe_path", slow_probe)
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)

    r = c.post("/api/collect", json={"path": str(scan_root)})
    task_id = r.json()["task_id"]
    assert probe_started.wait(5)

    cr = c.post(f"/api/tasks/{task_id}/cancel")
    assert cr.status_code == 200
    assert cr.json()["status"] == "cancelling"
    # 已终态任务不可再取消 → 409；未知任务 → 404
    gate.set()
    task = _wait_task(c, task_id)
    assert task["status"] == "cancelled"
    assert c.post(f"/api/tasks/{task_id}/cancel").status_code == 409
    assert c.post("/api/tasks/task_nope/cancel").status_code == 404
    # 未封库目标目录已清理
    vol_dir = tmp_path / "dataroot" / "SERFAKE123_P1"
    assert not vol_dir.exists() or list(vol_dir.iterdir()) == []


def test_cancel_pending_collect(client, tmp_path: Path, monkeypatch) -> None:
    """排队中取消 → 直接落 cancelled（工作线程跳过）。"""
    c, _ = client
    gate = threading.Event()

    def slow_probe(path, *, manual_serial=None, smartctl=True):
        gate.wait(10)
        return _fake_probe()(path, manual_serial=manual_serial, smartctl=smartctl)

    monkeypatch.setattr("cold_manifest.collect.probe_path", slow_probe)
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)

    r1 = c.post("/api/collect", json={"path": str(scan_root)})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if c.get(f"/api/tasks/{r1.json()['task_id']}").json()["status"] == "running":
            break
        time.sleep(0.02)
    # 第二个任务排队中（不同路径避免 409）
    r2 = c.post("/api/collect", json={"path": str(scan_root / "d0"),
                                      "volume_id": "SEROTHER_P1"})
    assert r2.status_code == 201
    cr = c.post(f"/api/tasks/{r2.json()['task_id']}/cancel")
    assert cr.status_code == 200
    assert cr.json()["status"] == "cancelled"
    assert c.get(f"/api/tasks/{r2.json()['task_id']}").json()["status"] == "cancelled"
    gate.set()
    assert _wait_task(c, r1.json()["task_id"])["status"] == "done"
    # 被取消的 pending 任务不得被执行
    time.sleep(0.2)
    assert c.get(f"/api/tasks/{r2.json()['task_id']}").json()["status"] == "cancelled"


def test_engine_cancel_cleans_up(tmp_path: Path, monkeypatch) -> None:
    """引擎级取消：CollectCancelled + 未封库目录/盘上 tmp 清理。"""
    monkeypatch.setattr("cold_manifest.collect.probe_path", _fake_probe())
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    ev = threading.Event()
    ev.set()
    with pytest.raises(CollectCancelled):
        collect_volume(scan_root, data_root=tmp_path / "data", cancel_event=ev)
    # 取消发生在 probe 后首个检查点：目标目录可能已建，必须已被清理
    vol_dir = tmp_path / "data" / "SERFAKE123_P1"
    assert not vol_dir.exists() or list(vol_dir.iterdir()) == []


# ---------------------------------------------------------------- SSE

def test_sse_frames_and_terminal(client, tmp_path: Path, monkeypatch) -> None:
    """SSE：读到 ≥3 帧合法 task JSON，终态后服务端关闭。"""
    c, _ = client
    gate = threading.Event()

    def slow_probe(path, *, manual_serial=None, smartctl=True):
        gate.wait(10)
        return _fake_probe()(path, manual_serial=manual_serial, smartctl=smartctl)

    monkeypatch.setattr("cold_manifest.collect.probe_path", slow_probe)
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    r = c.post("/api/collect", json={"path": str(scan_root)})
    task_id = r.json()["task_id"]

    frames: list[dict] = []
    with c.stream("GET", f"/api/tasks/{task_id}/events", params={"interval_ms": 100}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: "):]))
                if len(frames) >= 3:
                    break
    assert len(frames) == 3
    assert all(f["id"] == task_id for f in frames)
    assert all(f["status"] in ("pending", "running") for f in frames)

    gate.set()
    task = _wait_task(c, task_id)
    assert task["status"] == "done"
    # 终态后流只推最后一帧即关闭
    with c.stream("GET", f"/api/tasks/{task_id}/events") as resp:
        lines = [l for l in resp.iter_lines() if l.startswith("data: ")]
    assert len(lines) == 1
    assert json.loads(lines[0][len("data: "):])["status"] == "done"
    # 未知任务 → 404（建立流之前）
    assert c.get("/api/tasks/task_nope/events").status_code == 404


# ---------------------------------------------------------------- 孤儿清扫

def _write_collect_db(ts_dir: Path, sealed: bool) -> None:
    ts_dir.mkdir(parents=True)
    conn = sqlite3.connect(str(ts_dir / "snapshot.db"))
    conn.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        "CREATE TABLE entries(entry_id INTEGER PRIMARY KEY);"
    )
    conn.executemany("INSERT INTO meta(key, value) VALUES(?, ?)",
                     [("collector_version", "0.5.0"),
                      *([("status", "sealed")] if sealed else [])])
    conn.commit()
    conn.close()


def test_orphan_sweep_on_start(tmp_path: Path) -> None:
    """start() 清扫：未 sealed 的采集残留目录删除；sealed/外部库/空数据根保留。"""
    from cold_manifest.tasks import TaskRunner

    data_root = tmp_path / "dataroot"
    orphan = data_root / "VOLA_P0" / "20250101T000000Z"
    _write_collect_db(orphan, sealed=False)
    sealed = data_root / "VOLA_P0" / "20250201T000000Z"
    _write_collect_db(sealed, sealed=True)
    external = data_root / "VOLB_P0" / "20250301T000000Z"
    _write_collect_db(external, sealed=False)
    (external / "snapshot.db").unlink()  # 无 collector_version 的库 → 外部数据，不动
    (external / "snapshot.db").write_bytes(b"junk")
    empty = data_root / "VOLC_P0" / "20250401T000000Z"
    empty.mkdir(parents=True)
    diffs = data_root / "_diffs" / "x"
    diffs.mkdir(parents=True)

    runner = TaskRunner(data_root)
    runner.start()
    try:
        assert not orphan.exists()
        assert not empty.exists()
        assert sealed.exists()
        assert external.exists()
        assert diffs.exists()
    finally:
        runner.stop()


def test_sweep_keeps_unregistered_test_db(client, tmp_path: Path) -> None:
    """回归：catalog 已注册但未带 sealed meta 的合成库（测试/外部布局）不被误删。"""
    c, tmp = client
    # client 的 data_root 里放一个无 collector_version 的未 sealed 库
    keep = tmp / "dataroot" / "VOLX_P0" / "20250101T000000Z"
    _write_collect_db(keep, sealed=False)
    conn = sqlite3.connect(str(keep / "snapshot.db"))
    conn.execute("DELETE FROM meta WHERE key='collector_version'")
    conn.commit()
    conn.close()
    # 触发一次新的 TaskRunner 生命周期（新 app）
    app = create_app(str(tmp / "dataroot"))
    with TestClient(app):
        pass
    assert keep.exists()
