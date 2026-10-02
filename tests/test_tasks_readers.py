"""API 线程读路径与工作线程写连接解耦的测试。

背景：TaskRunner 的工作线程持有 self._conn（check_same_thread=False），
sqlite3.Connection 本身不是线程安全的——API 线程（GET /api/tasks 等）
与之并发使用同一条连接会偶发 sqlite3.InterfaceError。修复后读路径
（list_tasks/get_task/has_active_for/batch_child_statuses/get_batch）
每次调用开独立短连接，不再触碰 self._conn。

本文件不测具体业务语义（那些在 test_tasks_delete.py / test_collect_api.py），
只测并发正确性：
- 单元级：runner._conn 被替换成失效连接后读函数仍正常（旧实现下必炸）；
- 并发级：真实后台任务运行中，主线程高频调用读函数零异常；
- 生命周期：stop() 之后读函数优雅返回（不因连接已关而抛错）。
"""

import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.server import create_app
from cold_manifest.tasks import TaskRunner, register_task_fn
from test_collect import _fake_probe


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(str(tmp_path / "dataroot"))
    with TestClient(app) as c:
        yield c, tmp_path / "dataroot"


def _make_tree(root: Path, dirs: int = 2) -> None:
    for i in range(dirs):
        d = root / f"d{i}"
        d.mkdir(parents=True)
        (d / "a.txt").write_bytes(b"x" * 100)


def _probe_fn(payload, cb, ev):
    return {"payload": payload}


def _churn_fn(payload, cb, ev):
    for i in range(50):
        cb("step", i, 50)
        time.sleep(0.005)
    return "ok"


register_task_fn("unit_probe", _probe_fn)
register_task_fn("unit_churn", _churn_fn)


# ---------------------------------------------------------------- 单元级


def test_reads_work_when_shared_conn_broken(tmp_path: Path) -> None:
    """runner._conn 换成已关闭的失效连接后，读函数仍应正常工作。

    旧实现（读走 _conn）在这里抛 sqlite3.ProgrammingError；新实现
    （短连接读）完全不触碰 _conn，正常返回数据。
    """
    runner = TaskRunner(tmp_path / "dataroot")
    runner.start()
    try:
        tid = runner.submit("unit_probe", {"k": "v"})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            t = runner.get_task(tid)
            if t and t["status"] == "done":
                break
            time.sleep(0.02)
        assert runner.get_task(tid)["status"] == "done"

        # 换上失效连接（模拟"这条连接不能再被 API 线程用"）
        broken = sqlite3.connect(str(tmp_path / "catalog.db"))
        broken.close()
        old_conn = runner._conn
        runner._conn = broken
        try:
            tasks, cur, more = runner.list_tasks(limit=10)
            assert any(t["id"] == tid for t in tasks)
            assert cur is None and more is False
            assert runner.get_task(tid)["id"] == tid
            assert runner.has_active_for("nope", field="k") is False
            assert runner.batch_child_statuses("no-batch") == []
            assert runner.get_batch("no-batch") is None
        finally:
            runner._conn = old_conn
    finally:
        runner.stop()


def test_reads_use_independent_connection(tmp_path: Path, monkeypatch) -> None:
    """读函数不应复用 runner._conn：monkeypatch 记录 sqlite3.connect，
    读调用应各自开新短连接（且带着 uri=True / busy_timeout）。"""
    runner = TaskRunner(tmp_path / "dataroot")
    runner.start()
    try:
        runner.submit("unit_probe", {})
        time.sleep(0.2)

        real_connect = sqlite3.connect
        calls: "list[object]" = []

        def spy_connect(*args, **kwargs):
            calls.append(kwargs)
            return real_connect(*args, **kwargs)

        monkeypatch.setattr(sqlite3, "connect", spy_connect)
        runner.list_tasks(limit=5)
        runner.get_task("nope")
        # 两次读 = 至少两次新连接，且均显式 uri=True（约束：所有 connect 都带）
        assert len(calls) >= 2
        assert all(k.get("uri") is True for k in calls)
    finally:
        runner.stop()


# ---------------------------------------------------------------- 并发级


def test_concurrent_reads_during_running_task_no_errors(client, tmp_path: Path,
                                                        monkeypatch) -> None:
    """真实采集任务运行中，主线程高频调读函数数百次零异常。

    旧实现在高频轮询 + 工作线程写进度下会偶发 InterfaceError。
    """
    c, _ = client
    gate = threading.Event()

    def slow_probe(path, *, manual_serial=None, smartctl=True):
        gate.wait(10)
        return _fake_probe()(path, manual_serial=manual_serial, smartctl=smartctl)

    monkeypatch.setattr("cold_manifest.collect.probe_path", slow_probe)
    root = tmp_path / "vol"
    _make_tree(root)
    r = c.post("/api/collect", json={"path": str(root)})
    assert r.status_code == 201, r.text
    task_id = r.json()["task_id"]

    errors: "list[Exception]" = []

    def hammer():
        for _ in range(300):
            try:
                c.get("/api/tasks")
                c.get(f"/api/tasks/{task_id}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                return

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    # 让 hammer 先跑一会儿再放行任务
    time.sleep(0.3)
    gate.set()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    assert not errors, f"读路径并发异常：{errors[:3]}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        body = c.get(f"/api/tasks/{task_id}").json()
        if body["status"] in ("done", "error"):
            break
        time.sleep(0.05)
    assert body["status"] == "done", body


def test_reads_via_direct_runner_concurrent(tmp_path: Path) -> None:
    """不经 HTTP：工作线程执行带进度写的任务时，主线程直呼读函数零异常。"""
    runner = TaskRunner(tmp_path / "dataroot")
    runner.start()
    try:
        tid = runner.submit("unit_churn", {})
        for i in range(400):
            tasks, _, _ = runner.list_tasks(limit=10)
            t = runner.get_task(tid)
            assert t is not None
            runner.has_active_for("x", field="k")
            runner.batch_child_statuses("nope")
            if t["status"] == "done":
                break
            assert any(x["id"] == tid for x in tasks) or t["status"] in (
                "done", "error", "cancelled")
        # 400 轮高频读可能在任务结束前跑完（churn 要 ~0.25s），再等到终态
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            t = runner.get_task(tid)
            if t["status"] in ("done", "error", "cancelled"):
                break
            runner.list_tasks(limit=10)
            time.sleep(0.02)
        assert runner.get_task(tid)["status"] == "done"
    finally:
        runner.stop()


# ---------------------------------------------------------------- 生命周期


def test_reads_after_stop(tmp_path: Path) -> None:
    """stop() 之后（_conn 已关/置 None）读函数仍优雅返回，不抛异常。"""
    runner = TaskRunner(tmp_path / "dataroot")
    runner.start()
    tid = runner.submit("unit_probe", {"k": "v"})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        t = runner.get_task(tid)
        if t and t["status"] == "done":
            break
        time.sleep(0.02)
    runner.stop()
    assert runner._conn is None  # 写连接确已关闭

    # 读照常（短连接直接读盘上的 catalog.db）
    tasks, cur, more = runner.list_tasks(limit=10)
    assert any(t["id"] == tid for t in tasks)
    assert cur is None and more is False
    assert runner.get_task(tid)["status"] == "done"
    assert runner.has_active_for("nope", field="k") is False
    assert runner.batch_child_statuses("no-batch") == []
    assert runner.get_batch("no-batch") is None
