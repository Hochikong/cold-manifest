"""cldm collect 端到端测试：probe 注入夹具保证确定性。"""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from cold_manifest.catalog import connect_catalog
from cold_manifest.collect import CollectError, collect_volume, _is_sealed
from cold_manifest.probe import DiskInfo, VolumeInfo


def _fake_probe(mount: str = "/mnt/fake"):
    vol = VolumeInfo(
        filesystem="ext4", label="FAKELBL", volume_serial_hex="abcd-1234",
        partition_uuid="1111-2222", partition_index=1, partition_table_type="GPT",
        capacity_bytes=10_000_000_000, free_bytes=9_000_000_000,
        mount_point=mount, device_path="/dev/sdb1",
    )
    disk = DiskInfo(
        physical_model="Fake Disk 5000", physical_serial="SERFAKE123",
        disk_serial="SERFAKE123", serial_source="probe",
        bridge_model="", interface_type="SATA", capacity_bytes=20_000_000_000,
        firmware="fw1", smart_status="unavailable",
    )

    def probe(path, *, manual_serial=None, smartctl=True):
        if manual_serial is not None:
            disk.disk_serial = manual_serial
            disk.serial_source = "manual"
        return vol, disk

    return probe


def _open(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


@pytest.fixture()
def probe_env(monkeypatch):
    p = _fake_probe()
    monkeypatch.setattr("cold_manifest.collect.probe_path", p)
    return p


def _make_tree(root: Path) -> None:
    (root / "docs" / "sub").mkdir(parents=True)
    (root / "docs" / "a.txt").write_bytes(b"x" * 100)
    (root / "docs" / "weird, q.txt").write_bytes(b"y" * 200)
    (root / "docs" / "sub" / "b.bin").write_bytes(b"")
    (root / "emptydir").mkdir()
    (root / "skipme").mkdir()
    (root / "skipme" / "z.log").write_bytes(b"z" * 50)
    (root / "toplevel.log").write_bytes(b"t" * 10)


def test_collect_e2e(tmp_path: Path, probe_env) -> None:
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    data_root = tmp_path / "data"

    phases: list[tuple[str, int, "int | None"]] = []
    result = collect_volume(scan_root, data_root=data_root,
                            progress_cb=lambda ph, d, t: phases.append((ph, d, t)))

    assert result.snapshot_id.startswith("SERFAKE123_P1/")
    assert result.volume_id == "SERFAKE123_P1"
    assert result.files == 5
    # docs、docs/sub、emptydir、skipme（不含 root）
    assert result.dirs == 4
    assert result.total_bytes == 100 + 200 + 50 + 10
    assert result.warnings == []
    assert result.on_disk_path is not None and result.on_disk_path.is_file()
    assert result.on_disk_sha256 == result.host_sha256 == _sha256(result.db_path)
    # phase 顺序（每阶段有起止两次回调，取唯一相位）
    seen: list[str] = []
    for ph, _, _ in phases:
        if not seen or seen[-1] != ph:
            seen.append(ph)
    assert seen == ["probe", "scan", "seal", "copy", "register", "done"]

    # 主机库：封库 + 计数 + rollup 对账
    assert result.db_path.is_file()
    conn = _open(result.db_path)
    assert conn.execute("SELECT value FROM meta WHERE key='status'").fetchone()[0] == "sealed"
    assert conn.execute("SELECT COUNT(*) FROM entries WHERE type='file'").fetchone()[0] == 5
    rollup_root = conn.execute(
        "SELECT r.file_count, r.total_bytes FROM dir_rollup r JOIN entries e"
        " ON e.entry_id=r.entry_id WHERE e.path='.'").fetchone()
    assert rollup_root["file_count"] == 5
    assert rollup_root["total_bytes"] == result.total_bytes
    conn.close()

    # 盘上副本旁车
    sidecar = json.loads(result.on_disk_path.parent.joinpath("snapshot.json").read_text("utf-8"))
    assert sidecar["volume_id"] == "SERFAKE123_P1"
    assert sidecar["files"] == 5
    assert sidecar["total_bytes"] == result.total_bytes

    # catalog 五类行
    cat = connect_catalog(data_root)
    try:
        assert cat.execute("SELECT COUNT(*) FROM disks").fetchone()[0] == 1
        vol_row = cat.execute("SELECT * FROM volumes").fetchone()
        assert vol_row["volume_id"] == "SERFAKE123_P1"
        assert vol_row["partition_uuid"] == "1111-2222"
        snap = cat.execute("SELECT * FROM snapshots").fetchone()
        assert snap["snapshot_id"] == result.snapshot_id
        assert snap["status"] == "sealed"
        assert snap["file_count"] == 5
        assert snap["host_path"] == str(scan_root.resolve())
        copy_row = cat.execute("SELECT * FROM on_disk_copies").fetchone()
        assert copy_row["status"] == "ok"
        assert copy_row["sha256"] == result.host_sha256
        assert cat.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
    finally:
        cat.close()


def test_collect_no_space(tmp_path: Path, probe_env, monkeypatch) -> None:
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)

    import shutil as _shutil

    real = _shutil.disk_usage

    def fake_du(path):
        usage = real(path)
        return _shutil._ntuple_diskusage(usage.total, usage.used, 0)  # free=0

    monkeypatch.setattr("cold_manifest.collect.shutil.disk_usage", fake_du)
    result = collect_volume(scan_root, data_root=tmp_path / "data")

    assert result.on_disk_path is None
    assert any("no_space" in w for w in result.warnings)
    # catalog 记 skipped_no_space
    cat = connect_catalog(tmp_path / "data")
    try:
        assert cat.execute("SELECT status FROM on_disk_copies").fetchone()[0] == "skipped_no_space"
    finally:
        cat.close()
    # 盘上无 _coldmanifest 残留
    assert not (scan_root / "_coldmanifest").exists()


def test_collect_exclude_glob(tmp_path: Path, probe_env) -> None:
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    result = collect_volume(scan_root, data_root=tmp_path / "data",
                            exclude_globs=["*.log", "skipme"])

    assert result.files == 3  # a.txt / weird, q.txt / b.bin
    assert result.total_bytes == 300
    conn = _open(result.db_path)
    skipped = {(r["path"], r["warning_type"])
               for r in conn.execute("SELECT path, warning_type FROM skipped")}
    conn.close()
    # 目录命中整棵剪枝：skipme/z.log 不会被访问
    assert ("skipme", "excluded") in skipped
    assert ("toplevel.log", "excluded") in skipped
    assert ("skipme/z.log", "excluded") not in skipped


def test_cli_collect(tmp_path: Path, probe_env, capsys) -> None:
    from cold_manifest.cli import main

    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    rc = main(["collect", str(scan_root), "--data-root", str(tmp_path / "data")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "采集完成" in out
    assert "SERFAKE123_P1/" in out
    assert "文件：5" in out
    assert "主机 sha256" in out


def test_collect_missing_root(tmp_path: Path) -> None:
    with pytest.raises(CollectError):
        collect_volume(tmp_path / "nope", data_root=tmp_path / "data")


# ---- Gate P1.1a 修复对应测试 -------------------------------------------------

from datetime import datetime as _dt, timezone as _tz

from cold_manifest.schema import SNAPSHOT_TABLES_DDL


class _FrozenDatetime(_dt):
    """固定 ts，保证同秒重跑路径可确定性触发。"""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return _dt(2026, 1, 1, 12, 0, 0, tzinfo=_tz.utc)


def _write_unsealed_db(dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(dest_dir / "snapshot.db"))
    conn.executescript(SNAPSHOT_TABLES_DDL)
    # collector_version：标记为本工具采集产生的脏库（孤儿清扫口径 seal.is_collect_orphan）
    conn.execute("INSERT INTO meta(key, value) VALUES('collector_version', '0.0.0-test')")
    conn.execute("INSERT INTO meta(key, value) VALUES('status', 'draft')")
    conn.commit()
    conn.close()


def test_collect_existing_sealed_raises(tmp_path: Path, probe_env, monkeypatch) -> None:
    monkeypatch.setattr("cold_manifest.collect.datetime", _FrozenDatetime)
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    data_root = tmp_path / "data"
    collect_volume(scan_root, data_root=data_root)
    # 同秒重跑：目标已 sealed → 报错而非静默覆盖
    with pytest.raises(CollectError, match="已存在且已封库"):
        collect_volume(scan_root, data_root=data_root)


def test_collect_dirty_leftover_rebuilt(tmp_path: Path, probe_env, monkeypatch) -> None:
    monkeypatch.setattr("cold_manifest.collect.datetime", _FrozenDatetime)
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    data_root = tmp_path / "data"
    dest = data_root / "SERFAKE123_P1" / "20260101T120000Z"
    _write_unsealed_db(dest)
    (dest / "junk.tmp").write_bytes(b"x")

    result = collect_volume(scan_root, data_root=data_root)
    assert result.snapshot_id == "SERFAKE123_P1/20260101T120000Z"
    assert result.files == 5
    assert result.db_path.is_file()
    conn = _open(result.db_path)
    assert conn.execute("SELECT value FROM meta WHERE key='status'").fetchone()[0] == "sealed"
    assert not (dest / "junk.tmp").exists()
    conn.close()


def test_collect_sweeps_other_leftovers(tmp_path: Path, probe_env, monkeypatch) -> None:
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    data_root = tmp_path / "data"
    vol_root = data_root / "SERFAKE123_P1"
    # 未 sealed 的陈旧残留 → 应被清扫
    _write_unsealed_db(vol_root / "20250101T000000Z")
    # 已 sealed 的历史快照 → 保留
    sealed_dir = vol_root / "20250201T000000Z"
    _write_unsealed_db(sealed_dir)
    conn = sqlite3.connect(str(sealed_dir / "snapshot.db"))
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('status', 'sealed')")
    conn.commit()
    conn.close()

    result = collect_volume(scan_root, data_root=data_root)
    assert not (vol_root / "20250101T000000Z").exists()
    assert sealed_dir.is_dir()
    assert any(w.startswith("leftover_cleaned=") for w in result.warnings)


def test_collect_no_on_disk_copy_no_catalog_row(tmp_path: Path, probe_env) -> None:
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    result = collect_volume(scan_root, data_root=tmp_path / "data", on_disk_copy=False)
    assert result.on_disk_path is None
    assert result.warnings == []  # 不写 skipped_no_space
    assert not (scan_root / "_coldmanifest").exists()
    cat = connect_catalog(tmp_path / "data")
    try:
        assert cat.execute("SELECT COUNT(*) FROM on_disk_copies").fetchone()[0] == 0
    finally:
        cat.close()


def test_collect_prunes_coldmanifest_dir(tmp_path: Path, probe_env) -> None:
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    old = scan_root / "_coldmanifest" / "SERFAKE123_P1" / "20250101T000000Z"
    old.mkdir(parents=True)
    (old / "snapshot.db").write_bytes(b"old db bytes")
    (scan_root / "_coldmanifest" / "note.txt").write_text("meta")

    result = collect_volume(scan_root, data_root=tmp_path / "data")
    assert result.files == 5  # _coldmanifest 整棵不计
    conn = _open(result.db_path)
    paths = {r[0] for r in conn.execute("SELECT path FROM entries")}
    skipped = {(r[0], r[1]) for r in conn.execute("SELECT path, warning_type FROM skipped")}
    conn.close()
    assert not any(p.startswith("_coldmanifest") for p in paths)
    assert ("_coldmanifest", "excluded") in skipped


def test_collect_seal_failure_no_sealed_marker(tmp_path: Path, probe_env, monkeypatch) -> None:
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)

    def boom(_conn, progress=None):
        raise sqlite3.OperationalError("no such table: dir_rollup")

    monkeypatch.setattr("cold_manifest.collect.seal_snapshot", boom)
    with pytest.raises(CollectError, match="封库失败"):
        collect_volume(scan_root, data_root=tmp_path / "data")
    # 库文件留下但绝无 sealed 标记
    db = tmp_path / "data" / "SERFAKE123_P1"
    dbs = list(db.glob("*/snapshot.db"))
    assert len(dbs) == 1
    assert not _is_sealed(dbs[0])


def test_collect_cli_bad_volume_id_exit2(tmp_path: Path, probe_env, capsys) -> None:
    from cold_manifest.cli import main

    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    rc = main(["collect", str(scan_root), "--data-root", str(tmp_path / "data"),
               "--volume-id", "bad/id"])
    assert rc == 2
    assert "非法 volume_id" in capsys.readouterr().err


def test_collect_weird_serial_sanitized(tmp_path: Path, monkeypatch) -> None:
    p = _fake_probe()

    def probe(path, *, manual_serial=None, smartctl=True):
        vol, disk = p(path, manual_serial=manual_serial, smartctl=smartctl)
        disk.disk_serial = 'WD-40E: ZR/Z "x"'
        disk.physical_serial = ""  # 无物理序列号 → 探测值清洗后命名
        disk.serial_source = "probe"
        return vol, disk

    monkeypatch.setattr("cold_manifest.collect.probe_path", probe)
    scan_root = tmp_path / "vol"
    _make_tree(scan_root)
    result = collect_volume(scan_root, data_root=tmp_path / "data")
    assert result.volume_id == "WD-40E__ZR_Z__x__P1"


# ---- 序列号占位值回退（Windows 真盘验证 P1 修复回归）------------------------

def _fake_probe_serial(disk_serial: str, volume_serial_hex: str):
    vol = VolumeInfo(
        filesystem="ntfs", label="", volume_serial_hex=volume_serial_hex,
        partition_uuid="", partition_index=1, partition_table_type="MBR",
        capacity_bytes=1_000, free_bytes=500,
        mount_point="/mnt/fake", device_path="/dev/sdb1",
    )
    disk = DiskInfo(
        physical_model="", physical_serial="", disk_serial=disk_serial,
        serial_source="probe" if disk_serial else "",
        bridge_model="", interface_type="SATA",
        capacity_bytes=1_000, firmware="", smart_status="unavailable",
    )

    def probe(path, *, manual_serial=None, smartctl=True):
        if manual_serial is not None:
            disk.disk_serial = manual_serial
            disk.serial_source = "manual"
        return vol, disk

    return probe


def _collect_min(tmp_path, probe) -> object:
    scan_root = tmp_path / "vol"
    scan_root.mkdir()
    (scan_root / "a.txt").write_text("x")
    return collect_volume(scan_root, data_root=tmp_path / "data",
                          on_disk_copy=False, smartctl=False,
                          progress_cb=lambda ph, d, t: None)


def _meta(db: Path, key: str) -> str | None:
    conn = _open(db.parent / "snapshot.db")
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]
    finally:
        conn.close()


@pytest.mark.parametrize("placeholder", ["", "0", "0000000"])
def test_placeholder_serial_with_no_ata_serial_requires_manual(tmp_path, monkeypatch, placeholder):
    # 新规则：ATA 直通与系统枚举序列号都不可用 → 一律先拦（VOL- 卷序列号
    # 回退路径保留在代码里但不可达）
    from cold_manifest.collect import ManualSerialRequired
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _fake_probe_serial(placeholder, "c4a2126f"))
    with pytest.raises(ManualSerialRequired):
        _collect_min(tmp_path, _fake_probe_serial)


def test_serial_missing_entirely_errors(tmp_path, monkeypatch):
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _fake_probe_serial("0", ""))
    with pytest.raises(CollectError) as ei:
        _collect_min(tmp_path, _fake_probe_serial)
    assert "--serial" in str(ei.value)


def test_explicit_serial_still_wins_over_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _fake_probe_serial("0", "C4A2126F"))
    scan_root = tmp_path / "vol"
    scan_root.mkdir()
    (scan_root / "a.txt").write_text("x")
    result = collect_volume(scan_root, data_root=tmp_path / "data",
                            manual_serial="MYDISK123",
                            on_disk_copy=False, smartctl=False,
                            progress_cb=lambda ph, d, t: None)
    assert result.volume_id == "MYDISK123_P1"
    assert _meta(Path(result.db_path), "serial_source") == "manual"


def test_control_byte_serial_requires_manual(tmp_path, monkeypatch):
    # 廉价 USB 桥实测（KIOXIA TransMemory, F: 盘）：SerialNumber = '\x030'。
    # 新规则：控制字节残串不可用且 ATA 直通也没有 → 拦（不再走卷序列号回退）
    from cold_manifest.collect import ManualSerialRequired
    monkeypatch.setattr("cold_manifest.collect.probe_path",
                        _fake_probe_serial("\x030", "C4A2126F"))
    with pytest.raises(ManualSerialRequired):
        _collect_min(tmp_path, _fake_probe_serial)
