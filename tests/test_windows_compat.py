"""Windows 兼容修复 B 的单元测试。

覆盖：
- P0-3 catalog WAL 回退（journal_mode 返回值 ≠ wal → DELETE + synchronous=NORMAL）
- P1-1 控制台编码（PYTHONIOENCODING=gbk 下 CLI 中文输出不崩）
- P2-1 Windows 保留名/末尾点空格/ADS（scan_tree check_dangerous_names）
- P2-3 smartctl 设备路径（device_for_path 优先 PhysicalDrive<N>）
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from cold_manifest.db import open_catalog
from cold_manifest.scanner import scan_tree
from cold_manifest.smart import device_for_path

from test_scanner import MemWriter


# ---- P0-3 WAL 回退 -----------------------------------------------------------


def test_open_catalog_wal_on_ext4(tmp_path):
    """本机 FS（ext4/NTFS）支持 WAL：journal_mode 保持 wal，不打回退日志。"""
    db = tmp_path / "catalog.db"
    import logging

    records = []
    h = logging.Handler()
    h.emit = lambda r: records.append(r)
    logging.getLogger("cold_manifest.db").addHandler(h)
    try:
        conn = open_catalog(db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
    finally:
        logging.getLogger("cold_manifest.db").removeHandler(h)
    assert mode.lower() == "wal"
    assert records == []


def test_open_catalog_wal_fallback(tmp_path, monkeypatch, caplog):
    """模拟不支持 WAL 的 FS：PRAGMA journal_mode=WAL 返回非 wal → 回退 DELETE。"""
    db = tmp_path / "catalog.db"
    real_connect = sqlite3.connect

    class FakeConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *a):
            if sql == "PRAGMA journal_mode=WAL":
                # exFAT 上 WAL 静默失败：返回原模式 delete
                return self._conn.execute("PRAGMA journal_mode=DELETE")
            return self._conn.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def fake_connect(*a, **kw):
        return FakeConn(real_connect(*a, **kw))

    monkeypatch.setattr(sqlite3, "connect", fake_connect)
    with caplog.at_level("WARNING", logger="cold_manifest.db"):
        conn = open_catalog(db)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    assert mode.lower() == "delete"
    # NORMAL = 1（默认 2/FULL，回退后降为 NORMAL）
    assert sync == 1
    assert busy == 5000
    assert "不支持 WAL" in caplog.text


# ---- P1-1 控制台编码 ----------------------------------------------------------


def test_cli_gbk_console_no_crash(tmp_path):
    """PYTHONIOENCODING=gbk 下 CLI 也不因中文/特殊字符输出崩溃（P1-1）。"""
    repo = Path(__file__).resolve().parent.parent
    env = dict(os.environ, PYTHONIOENCODING="gbk")
    src = str(repo / "src")
    env.setdefault("PYTHONPATH", src)
    if src not in env["PYTHONPATH"]:
        env["PYTHONPATH"] = src + os.pathsep + env["PYTHONPATH"]
    # --help：描述含中文
    r1 = subprocess.run(
        [sys.executable, "-m", "cold_manifest.cli", "--help"],
        capture_output=True, env=env, timeout=60,
    )
    assert r1.returncode == 0, r1.stderr.decode("utf-8", "replace")
    # diff 摘要子命令 --help：usage/帮助文本含中文与箭头
    r2 = subprocess.run(
        [sys.executable, "-m", "cold_manifest.cli", "diff", "--help"],
        capture_output=True, env=env, timeout=60,
    )
    assert r2.returncode == 0, r2.stderr.decode("utf-8", "replace")
    assert "错误" not in r2.stderr.decode("gbk", "replace")


# ---- P2-1 Windows 保留名 ------------------------------------------------------


def _scan(tmp_path, **kw):
    w = MemWriter()
    st = scan_tree(tmp_path, w, **kw)
    return w, st


@pytest.mark.parametrize("name", ["CON", "con.txt", "NUL", "COM1", "lpt9.dat"])
def test_dangerous_reserved_names(tmp_path, name):
    (tmp_path / name).write_text("x")
    (tmp_path / "ok.txt").write_text("y")
    w, st = _scan(tmp_path, check_dangerous_names=True)
    kinds = {s[1] for s in w.skipped}
    assert "dangerous_name" in kinds
    # 保留名不产出条目
    assert all(e["name"] != name for e in w.entries)
    assert any(e["name"] == "ok.txt" for e in w.entries)
    assert st.skipped >= 1


@pytest.mark.parametrize("name", ["trail.", "trail ", "ads:stream", "a:b.txt"])
def test_dangerous_trailing_and_ads(tmp_path, name):
    try:
        (tmp_path / name).write_text("x")
    except OSError:
        pytest.skip(f"本 FS 无法创建名字 {name!r}")
    (tmp_path / "ok.txt").write_text("y")
    w, _ = _scan(tmp_path, check_dangerous_names=True)
    assert "dangerous_name" in {s[1] for s in w.skipped}
    assert all(e["name"] != name for e in w.entries)


def test_dangerous_names_off_on_linux(tmp_path):
    """Linux 默认关闭：含冒号/末尾空格的合法名字正常入库。"""
    name = "a:b.txt"
    (tmp_path / name).write_text("x")
    w, st = _scan(tmp_path)  # 默认 None → os.name=='nt' 才开
    if os.name == "nt":
        pytest.skip("Windows 上默认开启")
    assert all(s[1] != "dangerous_name" for s in w.skipped)
    assert any(e["name"] == name for e in w.entries)
    assert st.skipped == 0


def test_dangerous_dir_pruned(tmp_path):
    """保留名目录：整棵剪枝，仅记 skipped，不产出目录条目。"""
    d = tmp_path / "CON"
    d.mkdir()
    (d / "inner.txt").write_text("x")
    w, _ = _scan(tmp_path, check_dangerous_names=True)
    assert ("CON", "dangerous_name", "scan", None) in w.skipped
    assert all(e["name"] not in ("CON", "inner.txt") for e in w.entries)


# ---- P2-3 smartctl 设备路径 ----------------------------------------------------


def test_device_for_path_linux(tmp_path):
    """非 Windows 平台走 /proc/mounts 分支（本机可直接调用）。"""
    dev = device_for_path(tmp_path)
    assert dev is None or dev.startswith("/dev/")


def test_device_for_path_win(monkeypatch, tmp_path):
    """mock sys.platform=win32：优先 disk_index → PhysicalDrive<N>，退回盘符。"""
    from cold_manifest import smart

    monkeypatch.setattr(smart.sys, "platform", "win32")

    # 提供 disk_index → PhysicalDrive
    assert smart.device_for_path("E:\\data", disk_index=3) == "\\\\.\\PhysicalDrive3"
    assert smart.device_for_path("E:\\data", disk_index=0) == "\\\\.\\PhysicalDrive0"

    # 无 index → 退回盘符。mock Path.resolve 返回固定盘符路径
    import cold_manifest.smart as s

    class FakePath:
        def __init__(self, p):
            pass

        def resolve(self):
            return "E:\\data"

    monkeypatch.setattr(s, "Path", FakePath)
    assert s.device_for_path("anything") == "E:"
    # 非盘符形态 → None
    FakePath.resolve = lambda self: "unc\\share"
    assert s.device_for_path("anything") is None
