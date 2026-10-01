"""Windows 路径/URI 兼容（P4.1-A）：file_uri 唯一构造器 + 入口归一化。

背景：Windows 上数据根常形如 `D:\\pkg\\bin\\..\\..\\data`（start.cmd 的
%~dp0 展开），未归一化路径手拼 file: URI 会让 SQLite 报
"unable to open database"（SQLite URI 解析不处理 `..` 段）。
本文件回归：
1. file_uri() 输出形态（file:/// 前缀、无 .. 段、百分号编码、mode=ro）；
2. 数据根含 `..` + 中文/空格目录时 collect → diff → duplicates → export
   全链路可跑（CLI 入口归一化）；
3. 快照库损坏/无法打开 → API 返回 400（detail 可读，不是 500），
   且 diff_runs 记 error。
"""

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import connect_catalog, snapshot_path
from cold_manifest.cli import main as cli_main
from cold_manifest.db import file_uri

# ---------------------------------------------------------------- file_uri


def test_file_uri_shape(tmp_path):
    d = tmp_path / "带 空格" / "中文目录"
    d.mkdir(parents=True)
    uri = file_uri(d / "x.db", immutable=True)
    assert uri.startswith("file:///")
    assert ".." not in uri
    assert "%20" in uri            # 空格已编码
    assert "%E4%B8%AD" in uri      # 中文已编码（"中" UTF-8 首字节）
    assert "mode=ro" in uri
    assert "immutable=1" in uri
    # "中文目录" 整段不应以未编码形式出现
    assert "中文目录" not in uri


def test_file_uri_not_immutable(tmp_path):
    uri = file_uri(tmp_path, immutable=False)
    assert "mode=ro" in uri
    assert "immutable=1" not in uri


def test_file_uri_resolves_dotdot(tmp_path):
    weird = tmp_path / "pkg" / "bin" / ".." / ".." / "data"
    uri = file_uri(weird)
    assert ".." not in uri
    assert str((tmp_path / "data").resolve()) in uri


def test_file_uri_relative_raises():
    with pytest.raises(ValueError, match="绝对路径"):
        file_uri("relative/path.db")


def test_open_snapshot_via_file_uri(tmp_path):
    import cold_manifest.db as dbm
    p = tmp_path / "a b" / "库.db"
    p.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE t(x)")
    conn.commit()
    conn.close()
    conn = dbm.open_snapshot(p)
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------- 全链路


class _SeqDatetime:
    """依次返回预置时间的 datetime 替身（两次 collect 得到不同 ts）。"""

    def __init__(self, stamps):
        self._stamps = list(stamps)

    def now(self, tz=None):
        return self._stamps.pop(0)

    def strptime(self, *a, **k):
        return datetime.strptime(*a, **k)


@pytest.fixture()
def weird_data_root(tmp_path):
    """形如 <tmp>/pkg/bin/../../data 的数据根 + 中文/空格场景。"""
    (tmp_path / "pkg" / "bin").mkdir(parents=True)
    return tmp_path / "pkg" / "bin" / ".." / ".." / "data"


def _make_tree(root: Path, tag: str) -> None:
    (root / "文档 目录").mkdir(parents=True)
    (root / "文档 目录" / f"{tag}.txt").write_bytes(b"hello " + tag.encode())
    (root / f"dup-{tag}.bin").write_bytes(tag.encode() * 64)


def _collect(cli, data_root, scan_root, stamps, monkeypatch, capsys):
    fake = _SeqDatetime(stamps)
    monkeypatch.setattr("cold_manifest.collect.datetime", fake)

    class _Vol:
        filesystem = "ext4"
        label = "LBL"
        volume_serial_hex = "abcd-1234"
        partition_uuid = "u"
        partition_index = 1
        partition_table_type = "GPT"
        capacity_bytes = 10**10
        free_bytes = 9 * 10**9
        mount_point = "/mnt/fake"
        device_path = "/dev/sdb1"

    class _Disk:
        physical_model = "M"
        physical_serial = "SER1"
        disk_serial = "SER1"
        serial_source = "probe"
        bridge_model = ""
        interface_type = "USB"
        capacity_bytes = 2 * 10**10
        firmware = "fw"
        smart_status = "unavailable"

    monkeypatch.setattr(
        "cold_manifest.collect.probe_path",
        lambda path, **kw: (_Vol(), _Disk()))
    rc = cli(["collect", str(scan_root), "--data-root", str(data_root)])
    assert rc == 0, capsys.readouterr().err
    return rc


def test_e2e_with_dotdot_and_unicode(weird_data_root, tmp_path, monkeypatch, capsys):
    """数据根含 `..`、扫描树含中文/空格目录：collect→diff→duplicates→export。"""
    cli = cli_main
    s1 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    s2 = datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

    scan_a = tmp_path / "源A"
    scan_b = tmp_path / "源B"
    _make_tree(scan_a, "a")
    _make_tree(scan_b, "b")
    _collect(cli, weird_data_root, scan_a, [s1], monkeypatch, capsys)
    _collect(cli, weird_data_root, scan_b, [s2], monkeypatch, capsys)

    sid_a = "SER1_P1/20260101T000000Z"
    sid_b = "SER1_P1/20260102T000000Z"
    resolved_root = weird_data_root.resolve()
    assert snapshot_path(resolved_root, sid_a).is_file()

    # diff（hash 开启，走 ATTACH + immutable file: URI）；退出码 1=有差异，0=无差异
    rc = cli(["diff", sid_a, sid_b, "--hash", "sha256",
              "--data-root", str(weird_data_root)])
    assert rc in (0, 1), capsys.readouterr().err

    # 两个快照哈希后 duplicates（路径带空格/中文）
    for sid in (sid_a, sid_b):
        rc = cli(["hash", sid, "--data-root", str(weird_data_root)])
        assert rc == 0, capsys.readouterr().err
    rc = cli(["duplicates", sid_a, "--min-size", "1",
              "--data-root", str(weird_data_root)])
    assert rc == 0, capsys.readouterr().err

    # export
    out = tmp_path / "out 导出.csv"
    rc = cli(["export", sid_a, "--data-root", str(weird_data_root),
              "--output", str(out)])
    assert rc == 0, capsys.readouterr().err
    assert out.is_file()


def test_cli_data_root_normalized(capsys):
    """--data-root 显式传入含 .. 的路径，main 入口统一 resolve。"""
    # 用 hash 命令的未注册报错来观测归一化后的路径
    rc = cli_main(["hash", "NOPE/X", "--data-root", "/tmp/./x/../root"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "/tmp/root" in err
    assert ".." not in err


# ---------------------------------------------------------------- 失败可读化


def _register_and_corrupt(data_root: Path) -> str:
    """造一个"存在但损坏"的封库快照库（非 SQLite 内容）。"""
    sid = "VOL/20260101T000000Z"
    db = snapshot_path(data_root, sid)
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"this is not a sqlite database" * 10)
    return sid


def test_api_diff_corrupt_db_400(tmp_path):
    """快照库损坏 → API 400 + 可读 detail（不是 500），diff_runs 记 error。"""
    from cold_manifest.server import create_app

    data_root = tmp_path / "data"
    data_root.mkdir()
    sid = _register_and_corrupt(data_root)

    with TestClient(create_app(data_root=str(data_root))) as client:
        r = client.post("/api/diffs", json={"a": sid, "b": sid,
                                            "hash": "sha256"})
        assert r.status_code == 400, r.text
        assert "无法" in r.json()["detail"]

    cat = connect_catalog(data_root)
    try:
        row = cat.execute(
            "SELECT status FROM diff_runs WHERE a=? AND b=?", (sid, sid)
        ).fetchone()
        assert row is not None and row[0] == "error"
    finally:
        cat.close()


def test_api_diff_missing_snapshot_404(tmp_path):
    from cold_manifest.server import create_app

    data_root = tmp_path / "data"
    data_root.mkdir()
    with TestClient(create_app(data_root=str(data_root))) as client:
        r = client.post("/api/diffs", json={"a": "NOPE/X", "b": "NOPE/Y"})
        assert r.status_code == 404, r.text
