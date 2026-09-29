"""P1.2b 断点续采（scan_journal）与 data_root 写锁测试。"""

import json
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cold_manifest.collect import CollectCancelled, collect_volume
from cold_manifest.lockfile import DataRootLock, LockBusy
from cold_manifest.scan_journal import (ScanJournal, journal_path,
                                        read_completed)
from cold_manifest.scanner import ScanCancelled
from cold_manifest.seal import is_collect_orphan, is_sealed
from cold_manifest.probe import DiskInfo, VolumeInfo


def _fake_probe():
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
    return lambda path, *, manual_serial=None, smartctl=True: (vol, disk)


@pytest.fixture()
def probe_env(monkeypatch):
    monkeypatch.setattr("cold_manifest.collect.probe_path", _fake_probe())


def _make_tree(root: Path, n_dirs: int = 6, files_per_dir: int = 4) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n_dirs):
        d = root / f"d{i:03d}"
        d.mkdir()
        for j in range(files_per_dir):
            (d / f"f{j:03d}.txt").write_bytes(bytes([i % 256, j]) * (j + 1))


def _open(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _snapshot_fingerprint(db: Path) -> tuple:
    """与 entry_id 无关的库内容指纹：entries/skipped/dir_rollup 按 path 对齐。"""
    conn = _open(db)
    try:
        entries = {
            (r["path"], r["parent_path"], r["type"], r["size_bytes"],
             r["allocated_bytes"], r["mtime_ns"], r["ext"])
            for r in conn.execute(
                "SELECT e.*, p.path AS parent_path FROM entries e"
                " LEFT JOIN entries p ON p.entry_id = e.parent_id"
                " WHERE e.path != '.'")
        }
        skipped = {
            (r["path"], r["warning_type"], r["stage"])
            for r in conn.execute("SELECT * FROM skipped")
        }
        rollup = {
            (r["path"], r["file_count"], r["dir_count"], r["total_bytes"],
             r["total_allocated"], r["max_mtime_ns"])
            for r in conn.execute(
                "SELECT e.path, r.* FROM dir_rollup r JOIN entries e"
                " ON e.entry_id = r.entry_id")
        }
        return entries, skipped, rollup
    finally:
        conn.close()


# ---- scan_journal 基础 -------------------------------------------------------

def test_journal_roundtrip(tmp_path: Path) -> None:
    j = ScanJournal(tmp_path)
    j.record("a")
    j.record("b/c")
    assert read_completed(tmp_path) == set()  # 未 flush 不落盘
    j.flush()
    assert read_completed(tmp_path) == {"a", "b/c"}
    j.record("d")
    j.close()
    assert read_completed(tmp_path) == {"a", "b/c", "d"}
    assert journal_path(tmp_path).name == "scan_journal.jsonl"


def test_journal_truncated_tail_dropped(tmp_path: Path) -> None:
    p = journal_path(tmp_path)
    p.write_text("a\nb\nc", encoding="utf-8")  # 末行不完整（写行中途被杀）
    assert read_completed(tmp_path) == {"a", "b"}
    p.write_text("", encoding="utf-8")
    assert read_completed(tmp_path) == set()
    assert read_completed(tmp_path / "nope") is None


# ---- 断点续采 ----------------------------------------------------------------

def test_interrupt_then_resume_reconciles_full_scan(tmp_path: Path, probe_env,
                                                    monkeypatch) -> None:
    """中断 → 续采：与一次完整扫描逐条对账一致。"""
    monkeypatch.setattr("cold_manifest.scan_journal.FLUSH_EVERY_DIRS", 10)
    scan_root = tmp_path / "vol"
    _make_tree(scan_root, n_dirs=300, files_per_dir=50)  # 15k 文件，进度回调必触发

    def boom(phase, done, total):
        if phase == "scan":
            raise ScanCancelled("中断注入")

    data_root = tmp_path / "data"
    with pytest.raises(CollectCancelled):
        collect_volume(scan_root, data_root=data_root, on_disk_copy=False,
                       progress_cb=boom)

    vol_root = data_root / "SERFAKE123_P1"
    ts_dirs = [d for d in vol_root.iterdir() if d.is_dir()]
    assert len(ts_dirs) == 1
    interrupted = ts_dirs[0]
    # 现场保留：未封库 + journal 存在（可续采，清扫不动）
    db = interrupted / "snapshot.db"
    assert db.is_file() and not is_sealed(db)
    assert journal_path(interrupted).is_file()
    partial = read_completed(interrupted)
    assert partial and len(partial) < 301  # 记了一部分，未全部完成
    assert is_collect_orphan(interrupted) is False

    # 续采完成
    result = collect_volume(scan_root, data_root=data_root, on_disk_copy=False,
                            resume=True)
    assert result.resumed is True
    assert result.snapshot_id == f"SERFAKE123_P1/{interrupted.name}"
    conn = _open(result.db_path)
    assert conn.execute(
        "SELECT value FROM meta WHERE key='resume_state'").fetchone()[0] == "complete"
    conn.close()

    # 对账：与另一次完整采集（独立 data_root）逐条一致
    full = collect_volume(scan_root, data_root=tmp_path / "data_full",
                          on_disk_copy=False)
    assert (result.files, result.dirs, result.total_bytes, result.skipped) == \
           (full.files, full.dirs, full.total_bytes, full.skipped)
    assert _snapshot_fingerprint(result.db_path) == _snapshot_fingerprint(full.db_path)


def test_resume_without_candidate_creates_fresh(tmp_path: Path, probe_env) -> None:
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    result = collect_volume(scan_root, data_root=tmp_path / "data",
                            on_disk_copy=False, resume=True)
    assert result.resumed is False
    assert is_sealed(result.db_path)


def test_resume_sealed_dir_not_reusable(tmp_path: Path, probe_env) -> None:
    """已封库的目录不可续采：resume 找不到候选 → 新建。"""
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    data_root = tmp_path / "data"
    first = collect_volume(scan_root, data_root=data_root, on_disk_copy=False)
    assert journal_path(first.db_path.parent).is_file()  # 封库后 journal 仍在
    time.sleep(1.1)  # 保证新 ts（同秒重跑会被"已存在且已封库"拦下）
    second = collect_volume(scan_root, data_root=data_root, on_disk_copy=False,
                            resume=True)
    assert second.resumed is False
    assert second.snapshot_id != first.snapshot_id


def test_sweep_keeps_resumable_deletes_journal_less(tmp_path: Path) -> None:
    """清扫口径：带 journal 的未封库保留；无 journal 的采集残留清理。"""
    from cold_manifest.tasks import TaskRunner

    data_root = tmp_path / "dataroot"

    def _mk(dir_path: Path, with_journal: bool) -> None:
        dir_path.mkdir(parents=True)
        conn = sqlite3.connect(str(dir_path / "snapshot.db"))
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES('collector_version', '0.0.0-test')")
        conn.execute("INSERT INTO meta VALUES('status', 'draft')")
        conn.commit()
        conn.close()
        if with_journal:
            (dir_path / "scan_journal.jsonl").write_text("a\n", encoding="utf-8")

    resumable = data_root / "VOLA_P0" / "20250101T000000Z"
    _mk(resumable, with_journal=True)
    orphan = data_root / "VOLA_P0" / "20250201T000000Z"
    _mk(orphan, with_journal=False)

    assert is_collect_orphan(resumable) is False
    assert is_collect_orphan(orphan) is True

    runner = TaskRunner(data_root)
    runner.start()
    try:
        assert resumable.is_dir()
        assert not orphan.exists()
    finally:
        runner.stop()


# ---- data_root 写锁 ----------------------------------------------------------

def test_lock_exclusive_then_reusable(tmp_path: Path) -> None:
    lock1 = DataRootLock(tmp_path)
    lock1.acquire()
    assert json.loads((tmp_path / ".cldm.lock").read_text("utf-8"))["pid"]
    lock2 = DataRootLock(tmp_path)
    with pytest.raises(LockBusy, match="data_root 被占用"):
        lock2.acquire()
    lock1.release()
    lock2.acquire()  # 释放后可再取
    lock2.release()


def test_lock_context_manager(tmp_path: Path) -> None:
    with DataRootLock(tmp_path):
        with pytest.raises(LockBusy):
            DataRootLock(tmp_path).acquire()
    lk = DataRootLock(tmp_path)
    lk.acquire()
    lk.release()


def test_lock_skip_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLDM_SKIP_LOCK", "1")
    lock1 = DataRootLock(tmp_path)
    lock1.acquire()
    lk2 = DataRootLock(tmp_path)  # 禁用后不互斥
    lk2.acquire()
    lk2.release()
    lock1.release()


def test_lock_two_processes(tmp_path: Path) -> None:
    """子进程持锁 → 本进程干净失败；子进程退出（OS 释放锁）→ 可再取。"""
    child_code = (
        "import sys, time;"
        "from cold_manifest.lockfile import DataRootLock;"
        "l = DataRootLock(sys.argv[1]); l.acquire();"
        "print('locked', flush=True); time.sleep(30)"
    )
    proc = subprocess.Popen([sys.executable, "-c", child_code, str(tmp_path)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        with pytest.raises(LockBusy, match=str(proc.pid)):
            DataRootLock(tmp_path).acquire()
    finally:
        proc.kill()
        proc.wait(timeout=10)
    time.sleep(0.1)
    lk = DataRootLock(tmp_path)  # 崩溃/被杀后 OS 级释放
    lk.acquire()
    lk.release()


def test_cli_collect_lock_busy_exit2(tmp_path: Path, probe_env, capsys) -> None:
    """CLI 争锁：清晰错误 + 退出码 2。"""
    from cold_manifest.cli import main

    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    holder = DataRootLock(tmp_path / "data")
    holder.acquire()
    try:
        rc = main(["collect", str(scan_root), "--data-root", str(tmp_path / "data")])
        assert rc == 2
        assert "data_root 被占用" in capsys.readouterr().err
    finally:
        holder.release()
    rc = main(["collect", str(scan_root), "--data-root", str(tmp_path / "data"),
               "--no-on-disk-copy"])
    assert rc == 0


def test_api_collect_lock_busy_task_error(tmp_path: Path, monkeypatch) -> None:
    """服务端争锁：collect 任务落 error，message 含"data_root 被占用"。"""
    from cold_manifest.api.routes_collect import _run_collect

    holder = DataRootLock(tmp_path)
    holder.acquire()
    try:
        with pytest.raises(Exception, match="data_root 被占用"):
            _run_collect({"path": str(tmp_path), "data_root": str(tmp_path)},
                         progress_cb=lambda *a: None)
    finally:
        holder.release()


def test_interrupted_then_resume_via_thread_cancel(tmp_path: Path, probe_env) -> None:
    """cancel_event 中断（copy 阶段之后不触发，scan 中途置位）→ 保留现场 → 续采。"""
    scan_root = tmp_path / "vol"
    _make_tree(scan_root, n_dirs=40, files_per_dir=5)
    data_root = tmp_path / "data"
    ev = threading.Event()

    def cb(phase, done, total):
        if phase == "scan" and done >= 50:
            ev.set()

    with pytest.raises(CollectCancelled):
        collect_volume(scan_root, data_root=data_root, on_disk_copy=False,
                       cancel_event=ev, progress_cb=cb)
    vol_root = data_root / "SERFAKE123_P1"
    assert any(journal_path(d).is_file() for d in vol_root.iterdir() if d.is_dir())
    result = collect_volume(scan_root, data_root=data_root, on_disk_copy=False,
                            resume=True)
    assert result.resumed and result.files == 200
