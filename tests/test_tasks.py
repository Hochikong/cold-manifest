"""TaskRunner 回归测试：取消与 worker 抢跑竞态（CAS 认领）、成功恒 done。"""

import json
import threading
import time
from pathlib import Path

from cold_manifest.db import init_catalog, open_catalog
from cold_manifest.tasks import TaskRunner, register_task_fn

import pytest


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    d = tmp_path / "dataroot"
    d.mkdir()
    conn = open_catalog(d / "catalog.db", check_same_thread=False)
    init_catalog(conn)
    conn.close()
    return d


def test_cancel_race_pending_never_runs(data_root: Path) -> None:
    """回归（M2）：pending 任务在 worker 认领（置 running）前被取消 →
    终态 cancelled，fn 不执行（CAS 认领 rowcount=0 → 跳过）。"""
    executed = threading.Event()

    def fn(payload, progress_cb, cancel_event):  # pragma: no cover — 不应执行
        executed.set()
        return {}

    release = threading.Event()

    def blocker(payload, progress_cb, cancel_event):
        release.wait(timeout=10.0)
        return {}

    register_task_fn("race_probe", fn)
    register_task_fn("race_blocker", blocker)

    # ① 直接验证 CAS 认领语义：仅 pending 可被置 running（第二次 rowcount=0）
    runner = TaskRunner(data_root)
    runner.start()
    try:
        conn = runner._conn
        tid = "task_cas_probe"
        conn.execute(
            "INSERT INTO tasks(task_id, kind, payload_json, status, created_at)"
            " VALUES(?,?,?,?,?)",
            (tid, "race_probe", json.dumps({}), "pending", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        cur = conn.execute(
            "UPDATE tasks SET status='running' WHERE task_id=? AND status='pending'", (tid,))
        assert cur.rowcount == 1  # 认领成功
        cur = conn.execute(
            "UPDATE tasks SET status='running' WHERE task_id=? AND status='pending'", (tid,))
        assert cur.rowcount == 0  # 已非 pending → 不可再认领（cancel 后不复活的关键）
        conn.execute("DELETE FROM tasks WHERE task_id=?", (tid,))
        conn.commit()

        # ② 端到端抢跑：先用 blocker 占住 worker（保证后续提交始终停在 pending），
        #    再批量提交+立刻取消——终态必须全部 cancelled，fn 从未执行
        _blocker_id = runner.submit("race_blocker", {})
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            bt = runner.get_task(_blocker_id)
            if bt and bt["status"] == "running":
                break
            time.sleep(0.02)
        for i in range(20):
            task_id = runner.submit("race_probe", {"i": i})
            assert runner.cancel(task_id) == "cancelled"
        release.set()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            rows = conn.execute(
                "SELECT status FROM tasks WHERE kind IN ('race_probe','race_blocker')").fetchall()
            if all(r["status"] in ("done", "error", "cancelled") for r in rows):
                break
            time.sleep(0.02)
        statuses = [r["status"] for r in conn.execute(
            "SELECT status FROM tasks WHERE kind='race_probe'").fetchall()]
        assert statuses and all(s == "cancelled" for s in statuses)
        assert not executed.is_set()
    finally:
        release.set()
        runner.stop()


def test_success_is_done_even_if_cancel_event_set(data_root: Path) -> None:
    """回归（S1）：fn 正常返回后恒 done，不因 cancel_event 置位改判 cancelled。"""

    def fn(payload, progress_cb, cancel_event):
        if cancel_event is not None:
            cancel_event.set()  # 模拟取消请求在 fn 收尾前到达但 fn 仍正常完成
        return {"ok": True}

    register_task_fn("success_probe", fn)

    runner = TaskRunner(data_root)
    runner.start()
    try:
        task_id = runner.submit("success_probe", {})
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            t = runner.get_task(task_id)
            if t["status"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.02)
        t = runner.get_task(task_id)
        assert t["status"] == "done"
        assert t["result"] == {"ok": True}
        assert t["error"] is None
    finally:
        runner.stop()


def test_submit_dedup_locked(data_root: Path) -> None:
    """submit_dedup：同 kind+field 已有 pending/running → 返回 None，不插行；
    终态后可再次提交。"""
    release = threading.Event()

    def fn(payload, progress_cb, cancel_event):
        release.wait(timeout=5.0)
        return {}

    register_task_fn("dedup_probe", fn)

    runner = TaskRunner(data_root)
    runner.start()
    try:
        t1 = runner.submit_dedup("dedup_probe", {"path": "/x"}, field="path")
        assert t1 is not None
        # 同 key：拒（None）；不同 key：过
        assert runner.submit_dedup("dedup_probe", {"path": "/x"}, field="path") is None
        assert runner.submit_dedup("dedup_probe", {"path": "/y"}, field="path") is not None
        # kind 隔离
        assert runner.submit_dedup("other_probe", {"path": "/x"}, field="path") is not None
        release.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            rows = runner._conn.execute(
                "SELECT status FROM tasks WHERE kind IN ('dedup_probe','other_probe')").fetchall()
            if rows and all(r["status"] in ("done", "error", "cancelled") for r in rows):
                break
            time.sleep(0.02)
        # 终态后同 key 可再次提交
        assert runner.submit_dedup("dedup_probe", {"path": "/x"}, field="path") is not None
    finally:
        runner.stop()
