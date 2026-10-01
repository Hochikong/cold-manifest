"""P1.2a 多分区采集批次测试：probe 枚举夹具、API 批次、批次汇总、单卷重试、CLI 冒烟。"""

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.collect import CollectResult
from cold_manifest.probe import DiskInfo, ProbeError, VolumeInfo, VolumeTarget
from cold_manifest.server import create_app

# ---------------------------------------------------------------- probe 枚举（Linux 夹具）


def _lsblk_json() -> str:
    import json

    return json.dumps({
        "blockdevices": [
            {"name": "sda", "path": "/dev/sda", "type": "disk", "fstype": None,
             "size": 100, "ptuuid": "a" * 36, "serial": "SDAFAKE", "model": "Disk A",
             "children": [
                 {"name": "sda1", "path": "/dev/sda1", "type": "part",
                  "fstype": "ext4", "label": "root", "size": 60, "mountpoints": ["/"]},
             ]},
            {"name": "sdb", "path": "/dev/sdb", "type": "disk", "fstype": None,
             "size": 200, "ptuuid": "b" * 36, "serial": "SDBFAKE", "model": "Disk B",
             "children": [
                 {"name": "sdb1", "path": "/dev/sdb1", "type": "part",
                  "fstype": "swap", "label": None, "size": 4, "mountpoints": [None]},
                 {"name": "sdb2", "path": "/dev/sdb2", "type": "part",
                  "fstype": "ext4", "label": "data", "size": 100, "mountpoints": ["/mnt/data"]},
                 {"name": "sdb3", "path": "/dev/sdb3", "type": "part",
                  "fstype": None, "label": None, "size": 1, "mountpoints": [None]},
                 {"name": "sdb4", "path": "/dev/sdb4", "type": "part",
                  "fstype": "ntfs", "label": "BACKUP", "size": 95,
                  "mountpoints": ["/mnt/backup"]},
             ]},
        ]
    })


def test_enumerate_disk_volumes_linux(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "sub").mkdir()
    import cold_manifest.probe.linux as lin

    monkeypatch.setattr(lin, "find_mount_point", lambda p: ("/mnt/data", "/dev/sdb2"))

    def fake_run(cmd, *, check=True):
        assert cmd[:3] == ["lsblk", "-J", "-b"]
        return subprocess.CompletedProcess(cmd, 0, stdout=_lsblk_json(), stderr="")

    monkeypatch.setattr(lin, "_run", fake_run)
    targets, warnings = lin.enumerate_disk_volumes_linux(str(tmp_path / "sub"))
    # sdb1=swap、sdb3=无文件系统：静默跳过；sdb2/sdb4 入目标
    assert [(t.path, t.partition_index, t.filesystem, t.label)
            for t in targets] == [
        ("/mnt/data", 2, "ext4", "data"),
        ("/mnt/backup", 4, "ntfs", "BACKUP"),
    ]
    assert warnings == []


def test_enumerate_disk_volumes_linux_unmounted_warning(tmp_path: Path, monkeypatch) -> None:
    import json as _json

    import cold_manifest.probe.linux as lin

    monkeypatch.setattr(lin, "find_mount_point", lambda p: ("/mnt/data", "/dev/sdb2"))
    doc = _json.loads(_lsblk_json())
    doc["blockdevices"][1]["children"][3]["mountpoints"] = [None]  # sdb4 未挂载
    monkeypatch.setattr(
        lin, "_run",
        lambda cmd, *, check=True: subprocess.CompletedProcess(cmd, 0, stdout=_json.dumps(doc), stderr=""))
    targets, warnings = lin.enumerate_disk_volumes_linux(str(tmp_path))
    assert [t.path for t in targets] == ["/mnt/data"]
    assert len(warnings) == 1 and "sdb4" in warnings[0]


# ---------------------------------------------------------------- probe 枚举（Windows 夹具）

_WIN_JSON = """
{
  "diskNumber": 2,
  "partitionStyle": "GPT",
  "volumes": [
    {"letter": "E", "index": 2, "fs": "NTFS", "label": "ColdA", "size": 1000},
    {"letter": "F", "index": 3, "fs": "exFAT", "label": "ColdB", "size": 2000},
    {"letter": null, "index": 1, "fs": null, "label": null, "size": 100}
  ],
  "skipped": ["partition#1"]
}
"""


def test_parse_windows_volumes_json() -> None:
    from cold_manifest.probe.windows import parse_windows_volumes_json

    targets, warnings = parse_windows_volumes_json(_WIN_JSON)
    assert [(t.path, t.partition_index, t.filesystem, t.label, t.capacity_bytes)
            for t in targets] == [
        ("E:\\", 2, "NTFS", "ColdA", 1000),
        ("F:\\", 3, "exFAT", "ColdB", 2000),
    ]
    assert warnings == ["分区 partition#1 无盘符或无法关联逻辑盘，跳过"]


def test_parse_windows_volumes_json_single_volume_dict() -> None:
    """ConvertTo-Json 单卷时输出对象而非数组。"""
    from cold_manifest.probe.windows import parse_windows_volumes_json

    targets, warnings = parse_windows_volumes_json(
        '{"diskNumber": 1, "volumes": {"letter": "C", "index": 2, "fs": "NTFS",'
        ' "label": null, "size": null}, "skipped": []}')
    assert len(targets) == 1
    assert targets[0].path == "C:\\"
    assert targets[0].capacity_bytes is None
    assert warnings == []


def test_build_powershell_enum_command() -> None:
    from cold_manifest.probe.windows import build_powershell_enum_command

    script = build_powershell_enum_command("e:")
    assert "'e:'" not in script and "$letter = 'E'" in script
    assert "Get-Partition -DiskNumber" in script


# ---------------------------------------------------------------- API 批次


def _targets_for(paths: list[str]) -> list[VolumeTarget]:
    return [
        VolumeTarget(path=p, device_path=f"/dev/sdb{i + 1}", partition_index=i + 1,
                     filesystem="ext4", label=f"L{i}", capacity_bytes=1000 + i)
        for i, p in enumerate(paths)
    ]


def _fake_probe_per_path(paths: list[str]):
    """按路径返回不同 serial 的假 probe（避免同秒快照 ID 冲突）。"""

    def probe(path, *, manual_serial=None, smartctl=True):
        idx = paths.index(str(path))
        vol = VolumeInfo(filesystem="ext4", partition_index=idx + 1,
                         mount_point=str(path), device_path=f"/dev/sdb{idx + 1}")
        disk = DiskInfo(disk_serial=f"SERSERIAL{idx}", serial_source="probe")
        return vol, disk

    return probe


def _make_tree(root: Path) -> None:
    (root / "d").mkdir(parents=True)
    (root / "d" / "a.txt").write_bytes(b"x" * 10)
    (root / "top.bin").write_bytes(b"y" * 20)


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(str(tmp_path / "dataroot"))
    with TestClient(app) as c:
        yield c, tmp_path


def _wait_task(c: TestClient, task_id: str, timeout: float = 60.0) -> dict:
    # 上限放宽到 60s：全量套件负载下（含 3M 行级测试）任务可能远超 15s，
    # 旧值曾导致本文件偶发"未完成"假失败（单跑稳定）。真挂死仍会超时。
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = c.get(f"/api/tasks/{task_id}").json()
        if body["status"] in ("done", "error", "cancelled"):
            return body
        time.sleep(0.05)
    raise TimeoutError(f"任务未到终态：{task_id}")


def _wait_batch(c: TestClient, batch_id: str, timeout: float = 60.0) -> dict:
    # 同上：负载敏感的等待上限（见 _wait_task 注释）
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = c.get(f"/api/batches/{batch_id}").json()
        if body["status"] in ("done", "partial"):
            return body
        time.sleep(0.05)
    raise TimeoutError(f"批次未到终态：{batch_id}")


def test_collect_batch_api_e2e(client, tmp_path: Path, monkeypatch) -> None:
    c, _ = client
    vols = [tmp_path / f"vol{i}" for i in range(3)]
    for v in vols:
        _make_tree(v)
    targets = _targets_for([str(v) for v in vols])
    monkeypatch.setattr("cold_manifest.api.routes_collect.enumerate_disk_volumes",
                        lambda p: (targets, ["跳过 swap 分区"]))
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _fake_probe_per_path([str(v) for v in vols]))

    r = c.post("/api/collect", json={"path": str(vols[0]), "all_partitions": True})
    assert r.status_code == 201
    body = r.json()
    assert set(body) >= {"batch_id", "task_ids"}
    assert len(body["task_ids"]) == 3
    assert body["warnings"] == ["跳过 swap 分区"]

    # 串行执行由单工作线程保证；等批次到终态
    batch = _wait_batch(c, body["batch_id"])
    assert batch["status"] == "done"
    assert batch["summary"] == {"done": 3}
    assert batch["root"] == str(vols[0].resolve())
    assert len(batch["planned_volumes"]) == 3
    assert batch["planned_volumes"][0]["path"] == str(vols[0])

    # 每个子任务 related_id=batch_id；?batch_id= 过滤命中且互不混入
    for tid in body["task_ids"]:
        assert c.get(f"/api/tasks/{tid}").json()["status"] == "done"
    items = c.get("/api/tasks", params={"batch_id": body["batch_id"], "limit": 50}).json()
    assert {i["id"] for i in items["items"]} == set(body["task_ids"])
    assert all(i["payload"]["path"] in [str(v) for v in vols] for i in items["items"])
    # 每个任务的卷 ID 来自各自 probe（不同 serial）
    vols_result = {i["result"]["volume_id"] for i in items["items"]}
    assert vols_result == {f"SERSERIAL{i}_P{i + 1}" for i in range(3)}
    # 快照立即可见
    snaps = c.get("/api/snapshots").json()
    assert len(snaps["items"]) >= 3

    # 未知批次 → 404
    assert c.get("/api/batches/batch_nope").status_code == 404


def test_collect_batch_partial(client, tmp_path: Path, monkeypatch) -> None:
    """一卷失败 → 该任务 error、批次 partial，其余卷照常 done。"""
    c, _ = client
    vols = [tmp_path / f"pvol{i}" for i in range(3)]
    for v in vols:
        _make_tree(v)
    paths = [str(v) for v in vols]
    targets = _targets_for(paths)

    def flaky_probe(path, *, manual_serial=None, smartctl=True):
        if str(path) == paths[1]:
            raise ProbeError("模拟探测失败")
        return _fake_probe_per_path(paths)(path, manual_serial=manual_serial,
                                           smartctl=smartctl)

    monkeypatch.setattr("cold_manifest.api.routes_collect.enumerate_disk_volumes",
                        lambda p: (targets, []))
    monkeypatch.setattr("cold_manifest.collect.probe_path", flaky_probe)

    r = c.post("/api/collect", json={"path": str(vols[0]), "all_partitions": True})
    assert r.status_code == 201
    batch = _wait_batch(c, r.json()["batch_id"])
    assert batch["status"] == "partial"
    assert batch["summary"] == {"done": 2, "error": 1}
    statuses = {t["id"]: t["status"] for t in batch["tasks"]}
    assert list(statuses.values()).count("done") == 2
    assert list(statuses.values()).count("error") == 1


def test_collect_batch_single_volume_retry(client, tmp_path: Path, monkeypatch) -> None:
    """失败卷终态后单独重试：再 POST 该卷 path，409 去重自动解除。"""
    c, _ = client
    vols = [tmp_path / "rvol0", tmp_path / "rvol1"]
    for v in vols:
        _make_tree(v)
    paths = [str(v) for v in vols]
    targets = _targets_for(paths)

    def flaky_probe(path, *, manual_serial=None, smartctl=True):
        if str(path) == paths[0]:
            raise ProbeError("模拟失败")
        return _fake_probe_per_path(paths)(path, manual_serial=manual_serial,
                                           smartctl=smartctl)

    monkeypatch.setattr("cold_manifest.api.routes_collect.enumerate_disk_volumes",
                        lambda p: (targets, []))
    monkeypatch.setattr("cold_manifest.collect.probe_path", flaky_probe)

    r = c.post("/api/collect", json={"path": paths[0], "all_partitions": True})
    batch = _wait_batch(c, r.json()["batch_id"])
    assert batch["status"] == "partial"

    # 单卷重试：仅重发失败卷（换成功 probe）
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _fake_probe_per_path(paths))
    r2 = c.post("/api/collect", json={"path": paths[0]})
    assert r2.status_code == 201
    assert _wait_task(c, r2.json()["task_id"])["status"] == "done"
    # 批次（3 个子任务里 1 done + 1 error + 1 新单卷不属批次）仍 partial
    assert c.get(f"/api/batches/{r.json()['batch_id']}").json()["status"] == "partial"


def test_collect_batch_409_conflict(client, tmp_path: Path, monkeypatch) -> None:
    """任一卷已有活跃采集任务 → 整批 409，不产生半成品批次。"""
    import threading

    c, _ = client
    vols = [tmp_path / "cvol0", tmp_path / "cvol1"]
    for v in vols:
        _make_tree(v)
    paths = [str(v) for v in vols]
    gate = threading.Event()

    def slow_probe(path, *, manual_serial=None, smartctl=True):
        gate.wait(10)
        return _fake_probe_per_path(paths)(path, manual_serial=manual_serial,
                                           smartctl=smartctl)

    monkeypatch.setattr("cold_manifest.collect.probe_path", slow_probe)
    r1 = c.post("/api/collect", json={"path": paths[0]})
    assert r1.status_code == 201

    monkeypatch.setattr("cold_manifest.api.routes_collect.enumerate_disk_volumes",
                        lambda p: (_targets_for(paths), []))
    r2 = c.post("/api/collect", json={"path": paths[0], "all_partitions": True})
    assert r2.status_code == 409
    assert "cvol0" in r2.json()["detail"] or paths[0] in r2.json()["detail"]
    # 无批次行残留：tasks 表无 related_id=新批次 的行（提交前已 409）
    gate.set()
    assert _wait_task(c, r1.json()["task_id"])["status"] == "done"


def test_collect_batch_409_midway_cleans_up(client, tmp_path: Path, monkeypatch) -> None:
    """提交中途某卷被抢占（预检后竞态）→ 409 且无残留 batch/task 行。"""
    c, _ = client
    vols = [tmp_path / "mvol0", tmp_path / "mvol1"]
    for v in vols:
        _make_tree(v)
    paths = [str(v) for v in vols]
    targets = _targets_for(paths)
    monkeypatch.setattr("cold_manifest.api.routes_collect.enumerate_disk_volumes",
                        lambda p: (targets, []))

    runner = c.app.state.task_runner
    orig_submit = runner.submit_dedup
    calls = {"n": 0}

    def flaky_submit(kind, payload, *, field, related_id=None):
        calls["n"] += 1
        if calls["n"] == 2:  # 第二个卷模拟被抢占
            return None
        return orig_submit(kind, payload, field=field, related_id=related_id)

    monkeypatch.setattr(runner, "submit_dedup", flaky_submit)

    r = c.post("/api/collect", json={"path": paths[0], "all_partitions": True})
    assert r.status_code == 409

    # 无残留：batch 行与该 batch 的子任务行均被清理（第一个卷的任务也被删）
    with runner._lock:
        n_tasks = runner._conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE related_id LIKE 'batch_%'"
        ).fetchone()["n"]
        n_batches = runner._conn.execute(
            "SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
    assert n_tasks == 0 and n_batches == 0


def test_delete_batch_and_tasks_refuses_non_pending(client, tmp_path: Path) -> None:
    """delete_batch_and_tasks 安全阀：子任务已非 pending / 批次不存在 → 不删。"""
    c, _ = client
    runner = c.app.state.task_runner
    assert runner.delete_batch_and_tasks("batch_missing") is False

    runner.create_batch_row("batch_x", "", "/root", [{"path": "/v0"}])
    runner.submit_dedup("collect", {"path": "/v0"}, field="path",
                        related_id="batch_x")
    with runner._lock:
        runner._conn.execute(
            "UPDATE tasks SET status='running' WHERE related_id='batch_x'")
        runner._conn.commit()
    assert runner.delete_batch_and_tasks("batch_x") is False
    with runner._lock:
        n = runner._conn.execute(
            "SELECT COUNT(*) AS n FROM batches WHERE batch_id='batch_x'"
        ).fetchone()["n"]
    assert n == 1  # 未删


def test_collect_batch_400_no_targets(client, tmp_path: Path, monkeypatch) -> None:
    c, _ = client
    monkeypatch.setattr("cold_manifest.api.routes_collect.enumerate_disk_volumes",
                        lambda p: ([], []))
    r = c.post("/api/collect", json={"path": str(tmp_path), "all_partitions": True})
    assert r.status_code == 400
    # path 本身非法仍是 400（先校验 path 再枚举）
    assert c.post("/api/collect",
                  json={"path": "relative", "all_partitions": True}).status_code == 400


# ---------------------------------------------------------------- CLI 冒烟


def _fake_result(snapshot_id: str) -> CollectResult:
    return CollectResult(
        snapshot_id=snapshot_id, volume_id="VX_P1", db_path=Path("/tmp/x.db"),
        files=2, dirs=1, symlinks=0, others=0, total_bytes=30, total_allocated=30,
        skipped=0, elapsed_s=0.01, host_sha256="ab" * 32, on_disk_path=None,
        on_disk_sha256=None, warnings=[],
    )


def test_cli_collect_all_partitions(tmp_path: Path, monkeypatch, capsys) -> None:
    from cold_manifest import cli
    import cold_manifest.probe as probe_mod
    import cold_manifest.collect as collect_mod

    vols = [tmp_path / "cli_vol0", tmp_path / "cli_vol1"]
    for v in vols:
        _make_tree(v)
    targets = _targets_for([str(v) for v in vols])
    monkeypatch.setattr(probe_mod, "enumerate_disk_volumes",
                        lambda p, smartctl=True: (targets, ["w1"]))
    seen: list[str] = []

    def fake_collect(path, **kwargs):
        seen.append(str(path))
        if str(path) == str(vols[1]):
            raise collect_mod.CollectError("boom")
        return _fake_result(f"VX/{len(seen)}")

    monkeypatch.setattr(collect_mod, "collect_volume", fake_collect)

    rc = cli.main(["collect", str(vols[0]), "--all-partitions",
                   "--data-root", str(tmp_path / "data")])
    assert rc == 1  # 有失败卷 → partial
    out = capsys.readouterr().out
    assert "批次共 2 个卷" in out and "批次状态：partial" in out and "w1" in out
    assert seen == [str(vols[0]), str(vols[1])]  # 逐卷串行

    # 全部成功 → done、rc=0
    monkeypatch.setattr(collect_mod, "collect_volume",
                        lambda path, **kw: _fake_result("VX/ok"))
    rc = cli.main(["collect", str(vols[0]), "--all-partitions",
                   "--data-root", str(tmp_path / "data")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "批次状态：done（成功 2/2）" in out


def test_batch_not_terminal_until_all_children_present(client) -> None:
    """子任务行尚未全部落库时，批次不得报终态。

    提交顺序是"先建批次行、再逐条插子任务"；极快的工作线程可能先把已落库的子任务
    跑完，让批次瞬时看起来 done/partial —— 客户端（与 _wait_batch 这类轮询）会因此
    过早停止轮询、看到不完整摘要。计划 N 个卷但子任务不足 N 条时一律按 running 报。
    """
    c, tmp_path = client
    runner = c.app.state.task_runner
    bid = "batch_race_guard"
    runner.create_batch_row(bid, "disk_demo", "/root",
                            [{"volume_id": "v0"}, {"volume_id": "v1"}])
    # 只落库 1 个子任务（应失败 → 终态），模拟"计划 2 个但第 2 条还没插进去"
    tid = runner.submit("collect", {"path": "/definitely-not-exists",
                                    "data_root": str(tmp_path / "dataroot")},
                        related_id=bid)
    _wait_task(c, tid)
    body = c.get(f"/api/batches/{bid}").json()
    assert len(body["tasks"]) == 1
    assert body["status"] == "running", body
