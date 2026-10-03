"""昵称功能（P1）：迁移幂等、PATCH API、ensure_*/rebuild 不覆盖、报告/diff 带昵称。"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import (catalog_path, ensure_disk, ensure_volume,
                                   register_snapshot, snapshot_label,
                                   validate_volume_id)
from cold_manifest.db import init_catalog
from cold_manifest.rebuild import rebuild_catalog
from cold_manifest.schema import CATALOG_DDL, SNAPSHOT_DDL
from cold_manifest.server import create_app


# ---------------------------------------------------------------- 迁移


def test_migration_adds_nickname_and_idempotent(tmp_path: Path) -> None:
    """老库（无 nickname 列）跑 init_catalog 两次：列补齐且不报错。"""
    db = tmp_path / "catalog.db"
    conn = sqlite3.connect(str(db))
    # 手工造一个旧结构：无 nickname
    conn.executescript("""
        CREATE TABLE disks (
          disk_id TEXT PRIMARY KEY, physical_model TEXT, physical_serial TEXT,
          bridge_model TEXT, capacity_bytes INTEGER, interface_type TEXT,
          first_seen TEXT, last_seen TEXT, notes TEXT);
        CREATE TABLE volumes (
          volume_id TEXT PRIMARY KEY, disk_id TEXT NOT NULL REFERENCES disks,
          partition_index INTEGER, partition_uuid TEXT, volume_serial_hex TEXT,
          filesystem TEXT, label TEXT, capacity_bytes INTEGER, notes TEXT);
    """)
    conn.commit()
    init_catalog(conn)
    init_catalog(conn)  # 第二次：幂等
    for table in ("disks", "volumes"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "nickname" in cols
    conn.close()

    # 全新库：DDL 直接带 nickname
    conn2 = sqlite3.connect(":memory:")
    conn2.executescript(CATALOG_DDL)
    for table in ("disks", "volumes"):
        cols = {r[1] for r in conn2.execute(f"PRAGMA table_info({table})")}
        assert "nickname" in cols


# ---------------------------------------------------------------- fixture


DISK, VOL = "SN123", "SN123_P1"
A = f"{VOL}/20260101T000000Z"
B = f"{VOL}/20260202T000000Z"


def _mk_snapshot_db(data_root: Path, vol: str, ts: str) -> str:
    sid = f"{vol}/{ts}"
    db = data_root / vol / ts / "snapshot.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.executescript(SNAPSHOT_DDL)
    conn.execute("INSERT INTO meta(key, value) VALUES('status', 'sealed')")
    conn.execute("INSERT INTO meta(key, value) VALUES('volume_id', ?)", (vol,))
    conn.execute("INSERT INTO meta(key, value) VALUES('disk_serial', ?)", (DISK,))
    conn.executemany(
        "INSERT INTO entries(entry_id,parent_id,path,name,depth,type,size_bytes,"
        "allocated_bytes,mtime_ns) VALUES(?,?,?,?,?,?,?,?,?)",
        [(1, 0, ".", "", 0, "dir", None, None, None),
         (2, 1, "a.txt", ".", 1, "file", 5, 5, 100)])
    conn.commit()
    conn.close()
    return sid


@pytest.fixture()
def cat(tmp_path: Path) -> sqlite3.Connection:
    data_root = tmp_path / "data"
    data_root.mkdir()
    conn = sqlite3.connect(catalog_path(data_root))
    conn.row_factory = sqlite3.Row
    init_catalog(conn)
    yield conn
    conn.close()


@pytest.fixture()
def client(tmp_path: Path, cat: sqlite3.Connection) -> TestClient:
    data_root = tmp_path / "data"
    _mk_snapshot_db(data_root, VOL, "20260101T000000Z")
    _mk_snapshot_db(data_root, VOL, "20260202T000000Z")
    ensure_disk(cat, DISK)
    ensure_volume(cat, VOL, DISK)
    for sid in (A, B):
        register_snapshot(cat, sid, VOL, status="sealed", host_path="/t",
                          file_count=1, dir_count=0, total_bytes=5,
                          hash_policy="none")
    cat.commit()
    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c


# ---------------------------------------------------------------- PATCH API


def test_patch_disk_nickname(client: TestClient) -> None:
    r = client.patch(f"/api/disks/{DISK}", json={"nickname": "移动硬盘A"})
    assert r.status_code == 200
    assert r.json()["nickname"] == "移动硬盘A"

    # 更新
    r = client.patch(f"/api/disks/{DISK}", json={"nickname": "新名字"})
    assert r.json()["nickname"] == "新名字"

    # 清除：空串与 null
    assert client.patch(f"/api/disks/{DISK}", json={"nickname": ""})\
        .json()["nickname"] is None
    assert client.patch(f"/api/disks/{DISK}", json={"nickname": None})\
        .json()["nickname"] is None


def test_patch_volume_nickname(client: TestClient) -> None:
    r = client.patch(f"/api/volumes/{VOL}", json={"nickname": "视频分区"})
    assert r.status_code == 200
    assert r.json() == {"volume_id": VOL, "nickname": "视频分区"}
    # 清除
    assert client.patch(f"/api/volumes/{VOL}", json={"nickname": ""})\
        .json()["nickname"] is None


def test_patch_nickname_errors(client: TestClient) -> None:
    # 超长 → 400
    assert client.patch(f"/api/disks/{DISK}",
                        json={"nickname": "x" * 65}).status_code == 400
    assert client.patch(f"/api/volumes/{VOL}",
                        json={"nickname": "x" * 65}).status_code == 400
    # 64 字符正好可以
    assert client.patch(f"/api/disks/{DISK}",
                        json={"nickname": "x" * 64}).status_code == 200
    # 未知 disk → 404
    assert client.patch("/api/disks/NOPE",
                        json={"nickname": "a"}).status_code == 404
    # 未知卷 → 404；非法 volume_id → 400（直调层：httpx 会规范化 .. 路径）
    assert client.patch("/api/volumes/NOPE_X",
                        json={"nickname": "a"}).status_code == 404
    with pytest.raises(Exception):
        validate_volume_id("../evil")


# ---------------------------------------------------------------- 不覆盖


def test_ensure_disk_volume_do_not_overwrite(cat: sqlite3.Connection) -> None:
    ensure_disk(cat, DISK, physical_model="ModelX")
    ensure_volume(cat, VOL, DISK, filesystem="ntfs")
    cat.execute("UPDATE disks SET nickname='盘昵' WHERE disk_id=?", (DISK,))
    cat.execute("UPDATE volumes SET nickname='卷昵' WHERE volume_id=?", (VOL,))
    cat.commit()

    # 重复登记（含字段刷新）不覆盖昵称
    ensure_disk(cat, DISK, physical_model="ModelY")
    ensure_volume(cat, VOL, DISK, filesystem="exfat")
    cat.commit()
    assert cat.execute("SELECT nickname FROM disks WHERE disk_id=?",
                       (DISK,)).fetchone()[0] == "盘昵"
    assert cat.execute("SELECT nickname FROM volumes WHERE volume_id=?",
                       (VOL,)).fetchone()[0] == "卷昵"
    # 其它字段照常刷新
    assert cat.execute("SELECT filesystem FROM volumes WHERE volume_id=?",
                       (VOL,)).fetchone()[0] == "exfat"


def test_rebuild_keeps_nickname(tmp_path: Path, cat: sqlite3.Connection) -> None:
    data_root = tmp_path / "data"
    _mk_snapshot_db(data_root, VOL, "20260303T000000Z")
    # 预置昵称
    ensure_disk(cat, DISK)
    ensure_volume(cat, VOL, DISK)
    cat.execute("UPDATE disks SET nickname='盘昵' WHERE disk_id=?", (DISK,))
    cat.execute("UPDATE volumes SET nickname='卷昵' WHERE volume_id=?", (VOL,))
    cat.commit()

    res = rebuild_catalog(data_root)
    assert res["warnings"] == []
    assert cat.execute("SELECT nickname FROM disks WHERE disk_id=?",
                       (DISK,)).fetchone()[0] == "盘昵"
    assert cat.execute("SELECT nickname FROM volumes WHERE volume_id=?",
                       (VOL,)).fetchone()[0] == "卷昵"


# ---------------------------------------------------------------- 标签回落


def test_snapshot_label_fallback(cat: sqlite3.Connection) -> None:
    sid = A
    ensure_disk(cat, DISK)
    ensure_volume(cat, VOL, DISK)
    register_snapshot(cat, sid, VOL, status="sealed", host_path="/t")
    # 无任何昵称 → 空串
    assert snapshot_label(cat, sid) == ""
    # 仅盘昵称
    cat.execute("UPDATE disks SET nickname='移动硬盘A' WHERE disk_id=?", (DISK,))
    cat.commit()
    assert snapshot_label(cat, sid) == f"移动硬盘A · {VOL}"
    # 卷 + 盘
    cat.execute("UPDATE volumes SET nickname='视频分区' WHERE volume_id=?", (VOL,))
    cat.commit()
    assert snapshot_label(cat, sid) == f"视频分区（移动硬盘A） · {VOL}"
    # 未知快照 → 空串
    assert snapshot_label(cat, "NO/PE") == ""


# ---------------------------------------------------------------- diff 响应 / 报告


def _make_diff(client: TestClient) -> str:
    r = client.post("/api/diffs", json={"a": A, "b": B})
    assert r.status_code == 200
    return r.json()["diff_id"]


def test_diff_detail_summary_carry_labels(client: TestClient) -> None:
    client.patch(f"/api/volumes/{VOL}", json={"nickname": "视频分区"})
    client.patch(f"/api/disks/{DISK}", json={"nickname": "移动硬盘A"})
    did = _make_diff(client)
    for url in (f"/api/diffs/{did}", f"/api/diffs/{did}/summary"):
        labels = client.get(url).json()["labels"]
        assert labels == {"a": f"视频分区（移动硬盘A） · {VOL}",
                          "b": f"视频分区（移动硬盘A） · {VOL}"}
    # 无昵称 → 空串，不报错
    client.patch(f"/api/volumes/{VOL}", json={"nickname": None})
    client.patch(f"/api/disks/{DISK}", json={"nickname": None})
    did2 = _make_diff(client)
    assert client.get(f"/api/diffs/{did2}").json()["labels"] == {"a": "", "b": ""}


def test_diff_report_html_contains_label(client: TestClient) -> None:
    client.patch(f"/api/volumes/{VOL}", json={"nickname": "视频分区"})
    client.patch(f"/api/disks/{DISK}", json={"nickname": "移动硬盘A"})
    did = _make_diff(client)
    html = client.get(f"/api/diffs/{did}/report").text
    assert "视频分区（移动硬盘A）" in html
    # 回落：清掉卷昵称后只剩盘昵称，无空括号
    client.patch(f"/api/volumes/{VOL}", json={"nickname": None})
    did2 = _make_diff(client)
    html2 = client.get(f"/api/diffs/{did2}/report").text
    assert "移动硬盘A" in html2
    assert "（）" not in html2 and "( )" not in html2


# ---------------------------------------------------------------- 列表/详情/CLI 接入


def test_snapshot_list_and_detail_carry_nicknames(client: TestClient) -> None:
    # 未设昵称：NULL + label 空串
    body = client.get("/api/snapshots").json()
    for it in body["items"]:
        assert it["volume_nickname"] is None
        assert it["disk_nickname"] is None
        assert it["label"] == ""
    detail = client.get(f"/api/snapshots/{A}").json()
    assert detail["volume_nickname"] is None and detail["label"] == ""

    client.patch(f"/api/volumes/{VOL}", json={"nickname": "视频分区"})
    client.patch(f"/api/disks/{DISK}", json={"nickname": "移动硬盘A"})
    body = client.get("/api/snapshots").json()
    for it in body["items"]:
        assert it["volume_nickname"] == "视频分区"
        assert it["disk_nickname"] == "移动硬盘A"
        assert it["label"] == f"视频分区（移动硬盘A） · {VOL}"
    detail = client.get(f"/api/snapshots/{A}").json()
    assert detail["volume_nickname"] == "视频分区"
    assert detail["disk_nickname"] == "移动硬盘A"
    assert detail["label"] == f"视频分区（移动硬盘A） · {VOL}"
    # 仅盘昵称回落
    client.patch(f"/api/volumes/{VOL}", json={"nickname": None})
    it = client.get("/api/snapshots").json()["items"][0]
    assert it["label"] == f"移动硬盘A · {VOL}"


def test_disk_and_volume_responses_carry_nickname(client: TestClient) -> None:
    assert client.get("/api/disks").json()["items"][0]["nickname"] is None
    assert client.get(f"/api/disks/{DISK}").json()["nickname"] is None
    vols = client.get("/api/volumes").json()["items"]
    assert vols[0]["nickname"] is None and vols[0]["disk_nickname"] is None
    vd = client.get(f"/api/volumes/{VOL}").json()
    assert vd["nickname"] is None and vd["disk_nickname"] is None

    client.patch(f"/api/volumes/{VOL}", json={"nickname": "视频分区"})
    client.patch(f"/api/disks/{DISK}", json={"nickname": "移动硬盘A"})
    assert client.get("/api/disks").json()["items"][0]["nickname"] == "移动硬盘A"
    assert client.get(f"/api/disks/{DISK}").json()["nickname"] == "移动硬盘A"
    vols = client.get("/api/volumes").json()["items"]
    assert vols[0]["nickname"] == "视频分区"
    assert vols[0]["disk_nickname"] == "移动硬盘A"
    vd = client.get(f"/api/volumes/{VOL}").json()
    assert vd["nickname"] == "视频分区" and vd["disk_nickname"] == "移动硬盘A"


def _seed_diffs(cat: sqlite3.Connection, n: int) -> None:
    """直接插 diff_runs 行（POST /api/diffs 对同参幂等，凑不满 50+）。"""
    for i in range(n):
        cat.execute(
            "INSERT INTO diff_runs(diff_id,a,b,options_hash,created_at,status)"
            " VALUES(?,?,?,?,?,'done')",
            (f"d{i:03d}", A, B, f"h{i:03d}",
             f"2026-03-01T00:{i // 60:02d}:{i % 60:02d}Z"))
    cat.commit()


def test_diffs_list_pagination(client: TestClient, cat: sqlite3.Connection) -> None:
    client.patch(f"/api/volumes/{VOL}", json={"nickname": "视频分区"})
    _seed_diffs(cat, 55)

    r = client.get("/api/diffs")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"items", "has_more", "next_cursor"}
    assert len(body["items"]) == 50 and body["has_more"] is True
    assert body["next_cursor"]
    assert body["items"][0]["labels"] == {"a": f"视频分区 · {VOL}",
                                          "b": f"视频分区 · {VOL}"}
    created = [it["created_at"] for it in body["items"]]
    assert created == sorted(created, reverse=True)

    r2 = client.get("/api/diffs",
                    params={"limit": 50, "cursor": body["next_cursor"]})
    body2 = r2.json()
    assert len(body2["items"]) == 5
    assert body2["has_more"] is False and body2["next_cursor"] is None
    ids1 = {it["diff_id"] for it in body["items"]}
    ids2 = {it["diff_id"] for it in body2["items"]}
    assert not ids1 & ids2 and len(ids1 | ids2) == 55


def test_diffs_list_limit_and_cursor_errors(client: TestClient,
                                            cat: sqlite3.Connection) -> None:
    _seed_diffs(cat, 3)
    assert client.get("/api/diffs", params={"limit": 1}).json()["items"]
    assert client.get("/api/diffs", params={"limit": 200}).status_code == 200
    assert client.get("/api/diffs", params={"limit": 201}).status_code == 400
    assert client.get("/api/diffs", params={"limit": 0}).status_code == 400
    assert client.get("/api/diffs", params={"cursor": "not-a-cursor"}).status_code == 400


def test_cli_nickname(tmp_path: Path, cat: sqlite3.Connection) -> None:
    from cold_manifest.cli import main

    data_root = str((tmp_path / "data").resolve())
    ensure_disk(cat, DISK)
    ensure_volume(cat, VOL, DISK)
    cat.commit()

    # 设置
    assert main(["nickname", "disk", DISK, "移动硬盘A", "--data-root", data_root]) == 0
    assert main(["nickname", "volume", VOL, "视频分区", "--data-root", data_root]) == 0
    assert cat.execute("SELECT nickname FROM disks WHERE disk_id=?",
                       (DISK,)).fetchone()[0] == "移动硬盘A"
    assert cat.execute("SELECT nickname FROM volumes WHERE volume_id=?",
                       (VOL,)).fetchone()[0] == "视频分区"

    # 清除：空串与省略
    assert main(["nickname", "disk", DISK, "", "--data-root", data_root]) == 0
    assert main(["nickname", "volume", VOL, "--data-root", data_root]) == 0
    assert cat.execute("SELECT nickname FROM disks WHERE disk_id=?",
                       (DISK,)).fetchone()[0] is None
    assert cat.execute("SELECT nickname FROM volumes WHERE volume_id=?",
                       (VOL,)).fetchone()[0] is None

    # 错误：未知盘 / 未知卷 / 非法 volume_id / 超长 / 数据根不存在
    assert main(["nickname", "disk", "NOPE", "x", "--data-root", data_root]) == 2
    assert main(["nickname", "volume", "NOPE_X", "x", "--data-root", data_root]) == 2
    assert main(["nickname", "volume", "../evil", "x", "--data-root", data_root]) == 2
    assert main(["nickname", "disk", DISK, "x" * 65, "--data-root", data_root]) == 2
    assert main(["nickname", "disk", DISK, "x",
                 "--data-root", str(tmp_path / "nope")]) == 2
