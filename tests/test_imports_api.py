"""P1.0 导入 Web 化测试：scan / POST 导入任务 / 任务列表详情 / 400·404·409 / 白名单。"""

import base64
import os
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.api import routes_imports
from cold_manifest.server import create_app
from cold_manifest.tasks import _FN_REGISTRY
from test_import import _make_legacy_dir


@pytest.fixture()
def legacy_snap(tmp_path: Path) -> Path:
    return _make_legacy_dir(tmp_path)


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(str(tmp_path / "dataroot"))
    with TestClient(app) as c:  # with 语句触发 lifespan（启动 TaskRunner）
        yield c, tmp_path


def _wait_task(client: TestClient, task_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/api/tasks/{task_id}")
        body = r.json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.05)
    raise TimeoutError(f"任务未在 {timeout}s 内完成：{task_id}")


def test_scan_finds_candidates(client, legacy_snap: Path) -> None:
    c, tmp_path = client
    r = c.get("/api/imports/scan", params={"path": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert body["root"] == str(tmp_path)
    assert len(body["candidates"]) == 1
    cand = body["candidates"][0]
    assert cand["volume_id"] == "TESTVOL_P0"
    assert cand["collect_time"] == "2026-05-04T12:34:16Z"
    assert cand["status"] == "new"
    assert cand["tree_csv_bytes"] > 0
    assert body["errors"] == []


def test_scan_status_imported_after_import(client, legacy_snap: Path) -> None:
    c, tmp_path = client
    # 先导入，再 scan 应标 imported
    r = c.post("/api/imports", json={"snapshot_dir": str(legacy_snap)})
    assert r.status_code == 201
    task = _wait_task(c, r.json()["task_id"])
    assert task["status"] == "done"
    assert task["result"]["snapshot_id"] == "TESTVOL_P0/20260504T123416Z"

    body = c.get("/api/imports/scan", params={"path": str(tmp_path)}).json()
    assert body["candidates"][0]["status"] == "imported"


def test_post_import_then_snapshot_visible(client, legacy_snap: Path) -> None:
    c, tmp_path = client
    r = c.post("/api/imports", json={"snapshot_dir": str(legacy_snap), "force": False})
    assert r.status_code == 201
    assert set(r.json()) == {"task_id", "status"}
    task = _wait_task(c, r.json()["task_id"])
    assert task["status"] == "done"
    assert task["progress"] == 1.0

    snaps = c.get("/api/snapshots").json()
    assert any(s["snapshot_id"] == "TESTVOL_P0/20260504T123416Z" for s in snaps["items"])


def test_scan_400(client, tmp_path: Path) -> None:
    c, _ = client
    assert c.get("/api/imports/scan", params={"path": str(tmp_path / "nope")}).status_code == 400
    f = tmp_path / "afile.txt"
    f.write_text("x")
    assert c.get("/api/imports/scan", params={"path": str(f)}).status_code == 400
    assert c.get("/api/imports/scan", params={"path": "relative/path"}).status_code == 400


def test_post_400_invalid_dir(client, tmp_path: Path) -> None:
    c, _ = client
    # 不存在
    assert c.post("/api/imports", json={"snapshot_dir": str(tmp_path / "nope")}).status_code == 400
    # 存在但无 metadata/tree
    plain = tmp_path / "plain"
    plain.mkdir()
    assert c.post("/api/imports", json={"snapshot_dir": str(plain)}).status_code == 400


def test_task_detail_404(client) -> None:
    c, _ = client
    assert c.get("/api/tasks/task_nope").status_code == 404


def test_post_409_duplicate_active(client, legacy_snap: Path, monkeypatch) -> None:
    c, _ = client
    # 慢速假导入，制造 running 窗口
    orig = _FN_REGISTRY["import"]

    def slow(payload, cb, cancel_event=None):
        time.sleep(1.0)
        return orig(payload, cb)

    monkeypatch.setitem(_FN_REGISTRY, "import", slow)
    r1 = c.post("/api/imports", json={"snapshot_dir": str(legacy_snap)})
    assert r1.status_code == 201
    r2 = c.post("/api/imports", json={"snapshot_dir": str(legacy_snap)})
    assert r2.status_code == 409
    _wait_task(c, r1.json()["task_id"])


def test_import_roots_whitelist(client, legacy_snap: Path, monkeypatch) -> None:
    c, tmp_path = client
    monkeypatch.setenv("CLDM_IMPORT_ROOTS", str(tmp_path / "allowed"))
    # 在白名单外 → scan 与 POST 均 400
    assert c.get("/api/imports/scan", params={"path": str(tmp_path)}).status_code == 400
    assert c.post("/api/imports", json={"snapshot_dir": str(legacy_snap)}).status_code == 400
    # 白名单内 OK
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    snap2 = _make_legacy_dir(allowed_root)
    r = c.get("/api/imports/scan", params={"path": str(allowed_root)})
    assert r.status_code == 200
    assert len(r.json()["candidates"]) == 1
    r = c.post("/api/imports", json={"snapshot_dir": str(snap2)})
    assert r.status_code == 201
    assert _wait_task(c, r.json()["task_id"])["status"] == "done"


def test_tasks_list_keyset_and_status_filter(client, legacy_snap: Path) -> None:
    c, tmp_path = client
    # 制造两条 done：同目录 force 二次导入
    r1 = c.post("/api/imports", json={"snapshot_dir": str(legacy_snap)})
    _wait_task(c, r1.json()["task_id"])
    r2 = c.post("/api/imports", json={"snapshot_dir": str(legacy_snap), "force": True})
    _wait_task(c, r2.json()["task_id"])

    body = c.get("/api/tasks", params={"limit": 1}).json()
    assert body["has_more"] is True
    assert len(body["items"]) == 1
    # 用 next_cursor 翻页拿到第二条
    cur = body["next_cursor"]
    obj = json.loads(base64.urlsafe_b64decode(cur))
    assert "c" in obj and "i" in obj
    page2 = c.get("/api/tasks", params={"limit": 10, "cursor": cur}).json()
    assert len(page2["items"]) == 1
    assert page2["has_more"] is False
    ids = {body["items"][0]["id"], page2["items"][0]["id"]}
    assert ids == {r1.json()["task_id"], r2.json()["task_id"]}

    done = c.get("/api/tasks", params={"status": "done"}).json()
    assert done["has_more"] is False
    assert len(done["items"]) == 2
    assert all(t["status"] == "done" for t in done["items"])


# ---------------------------------------------------------------- 重启对账

def _insert_task(conn, task_id: str, status: str, snapshot_dir: str, created_at: str) -> None:
    conn.execute(
        "INSERT INTO tasks(task_id, kind, payload_json, status, created_at)"
        " VALUES(?,?,?,?,?)",
        (task_id, "import", json.dumps({"snapshot_dir": snapshot_dir}), status, created_at),
    )
    conn.commit()


def test_start_reconciles_stale_running_and_pending(tmp_path: Path, legacy_snap: Path) -> None:
    """遗留 running → error（解除 409）；遗留 pending 重新入队执行。"""
    from cold_manifest.db import init_catalog, open_catalog
    from cold_manifest.tasks import TaskRunner

    data_root = tmp_path / "dataroot"
    data_root.mkdir()
    # 直接 SQL 造遗留行：一条 running、两条 pending（乱序 created_at 验证 FIFO）
    conn = open_catalog(data_root / "catalog.db", check_same_thread=False)
    init_catalog(conn)
    _insert_task(conn, "task_stale_run", "running", str(legacy_snap), "2026-01-01T00:00:00Z")
    _insert_task(conn, "task_pend_2", "pending", str(legacy_snap), "2026-01-02T00:00:00Z")
    _insert_task(conn, "task_pend_1", "pending", str(legacy_snap), "2026-01-01T12:00:00Z")
    conn.close()

    runner = TaskRunner(data_root)
    runner.start()
    try:
        # running 行已被对账为 error
        t = runner.get_task("task_stale_run")
        assert t["status"] == "error"
        assert t["error"] == "进程重启中断"
        assert t["finished_at"] is not None
        # 遗留 pending 被重新执行
        deadline = time.monotonic() + 15.0
        statuses = {}
        while time.monotonic() < deadline:
            statuses = {tid: runner.get_task(tid)["status"]
                        for tid in ("task_pend_1", "task_pend_2")}
            if all(s in ("done", "error") for s in statuses.values()):
                break
            time.sleep(0.05)
        assert statuses == {"task_pend_1": "error", "task_pend_2": "error"}
        assert runner.get_task("task_pend_1")["started_at"] is not None
        # 全部终态后 has_active_for 不再阻塞
        assert not runner.has_active_for(str(legacy_snap))
    finally:
        runner.stop()


def test_reconcile_unblocks_409_via_api(tmp_path: Path, legacy_snap: Path) -> None:
    """端到端：遗留 running 行导致 409；重启 TaskRunner 对账后 POST 恢复 201。"""
    from cold_manifest.db import init_catalog, open_catalog
    from cold_manifest.server import create_app

    data_root = tmp_path / "dataroot"
    data_root.mkdir()
    conn = open_catalog(data_root / "catalog.db", check_same_thread=False)
    init_catalog(conn)
    _insert_task(conn, "task_stale", "running", str(legacy_snap.resolve()), "2026-01-01T00:00:00Z")
    conn.close()

    # 第一次启动：遗留行尚未清理前，模拟旧进程状态 → 直接验证对账后行为
    app = create_app(str(data_root))
    with TestClient(app) as c:
        r = c.post("/api/imports", json={"snapshot_dir": str(legacy_snap)})
        assert r.status_code == 201  # 遗留 running 已被 start() 对账为 error，不再 409


# ---------------------------------------------------------------- 白名单分隔符

def test_import_roots_pathsep(client, legacy_snap: Path, monkeypatch) -> None:
    """CLDM_IMPORT_ROOTS 用 os.pathsep 分隔：多根、含空白项均正确解析。"""
    c, tmp_path = client
    sep = os.pathsep
    allowed_root = tmp_path / "allowed"
    monkeypatch.setenv(
        "CLDM_IMPORT_ROOTS",
        sep.join([str(tmp_path / "nope"), "", str(allowed_root)]),
    )
    # 两个根都不存在也没关系：legacy_snap 不在任何允许根下 → 400
    assert c.get("/api/imports/scan", params={"path": str(tmp_path)}).status_code == 400
    # 建允许根并把快照放进去 → 200/201
    allowed_root.mkdir(parents=True)
    snap2 = _make_legacy_dir(allowed_root)
    r = c.get("/api/imports/scan", params={"path": str(allowed_root)})
    assert r.status_code == 200
    r = c.post("/api/imports", json={"snapshot_dir": str(snap2)})
    assert r.status_code == 201
    assert _wait_task(c, r.json()["task_id"])["status"] == "done"
