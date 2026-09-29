"""重复文件报告测试（P3-A）：分组正确性、过滤、keyset 分页、策略校验、HTML 转义。"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import catalog_path, ensure_disk, ensure_volume, register_snapshot
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.duplicates import (DEFAULT_MIN_SIZE, DuplicatesError,
                                      find_duplicates, render_duplicates_html)
from cold_manifest.server import create_app

SID = "vol/20260101T000000Z"


def _build_snapshot(data_root: Path, rows: "list[tuple]", meta: "dict[str, str]") -> str:
    """建封库快照（entries 含哈希列）并注册 catalog。rows 多一列 hash_hex。"""
    vol, ts = SID.split("/")
    db_dir = data_root / vol / ts
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_dir / "snapshot.db")
    init_snapshot(conn)
    conn.executemany(
        "INSERT INTO entries(entry_id, parent_id, path, name, depth, type, size_bytes,"
        " mtime_ns, path_norm, hash_algo, hash_hex, hash_state)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.executemany("INSERT INTO meta(key, value) VALUES(?,?)", list(meta.items()))
    conn.commit()
    conn.close()
    cat = sqlite3.connect(catalog_path(data_root))
    init_catalog(cat)
    ensure_disk(cat, "DISK1", physical_model="TEST")
    ensure_volume(cat, vol, "DISK1")
    register_snapshot(cat, SID, vol, status="sealed", host_path="/tmp/nowhere")
    cat.commit()
    cat.close()
    return SID


FULL_META = {"status": "sealed", "hash_policy": "full", "hash_algo": "sha256"}


def _row(eid, path, size, hh):
    name = path.rsplit("/", 1)[-1]
    depth = path.count("/") + 1
    return (eid, eid - 1, path, name, depth, "file", size, 1111, path,
            "sha256", hh, "full")


@pytest.fixture()
def env(tmp_path: Path) -> sqlite3.Connection:
    """标准夹具：
    - 组1：3 个 2MiB 同哈希文件（浪费 4MiB）
    - 组2：2 个 4MiB 同哈希文件（浪费 4MiB）
    - 组3：2 个 1MiB 同哈希文件（浪费 1MiB，<默认下限 1MiB 时被过滤是 ≥）
    - 干扰：2 个同大小（4MiB）不同哈希；1 个 5MiB 独文件；1 个 100B 重复小文件
    """
    rows = [
        (1, 0, ".", "", 0, "dir", None, None, None, None, None, None),
        _row(2, "a1.bin", 2 * 2**20, "H1"),
        _row(3, "a2.bin", 2 * 2**20, "H1"),
        _row(4, "sub/a3.bin", 2 * 2**20, "H1"),
        _row(5, "b1.bin", 4 * 2**20, "H2"),
        _row(6, "b2.bin", 4 * 2**20, "H2"),
        _row(7, "c1.bin", 2**20, "H3"),
        _row(8, "c2.bin", 2**20, "H3"),
        _row(9, "d1.bin", 4 * 2**20, "H4"),   # 同 b 组大小，不同内容
        _row(10, "d2.bin", 4 * 2**20, "H5"),
        _row(11, "solo.bin", 5 * 2**20, "H6"),  # 独文件不重复
        _row(12, "tiny1", 100, "H7"),
        _row(13, "tiny2", 100, "H7"),
    ]
    _build_snapshot(tmp_path, rows, FULL_META)
    conn = sqlite3.connect(f"file:{(tmp_path / 'vol' / '20260101T000000Z' /
                                    'snapshot.db').as_posix()}?mode=ro&immutable=1",
                           uri=True)
    yield conn
    conn.close()


# ---------------------------------------------------------------- 核心逻辑

def test_groups_and_wasted(env: sqlite3.Connection) -> None:
    """组数、浪费字节与手算一致：H1 3×2MiB 浪费 4MiB；H2 2×4MiB 浪费 4MiB；
    H3 2×1MiB 浪费 1MiB；tiny(100B) 低于默认下限被过滤。"""
    r = find_duplicates(env, SID)
    assert r["snapshot_id"] == SID
    assert r["hash_algo"] == "sha256"
    assert r["hashed_files"] == 12
    assert r["duplicate_groups"] == 3
    assert r["total_wasted_bytes"] == 4 * 2**20 + 4 * 2**20 + 2**20
    assert [it["hash_hex"] for it in r["items"]] == ["H2", "H1", "H3"]  # wasted 相同按 size 降序
    h1 = r["items"][1]
    assert h1["count"] == 3 and h1["wasted_bytes"] == 4 * 2**20
    assert h1["paths"] == ["a1.bin", "a2.bin", "sub/a3.bin"]
    assert not h1["paths_truncated"]


def test_min_size_boundary_and_filter(env: sqlite3.Connection) -> None:
    """≥ min_size（边界含）；下限 1MiB 时 H3（恰 1MiB）在列，2MiB 后消失。"""
    r = find_duplicates(env, SID, min_size=DEFAULT_MIN_SIZE)
    assert "H3" in [it["hash_hex"] for it in r["items"]]  # 边界：size == min_size
    assert "H7" not in [it["hash_hex"] for it in r["items"]]
    r2 = find_duplicates(env, SID, min_size=2 * 2**20)
    assert r2["duplicate_groups"] == 2
    assert r2["total_wasted_bytes"] == 8 * 2**20


def test_same_size_diff_hash_not_grouped(env: sqlite3.Connection) -> None:
    """同大小（4MiB）不同哈希的 d1/d2 不与 b 组成组；独文件不成组不出现。"""
    r = find_duplicates(env, SID, min_size=0)
    by = {it["hash_hex"]: it["count"] for it in r["items"]}
    assert by == {"H1": 3, "H2": 2, "H3": 2, "H7": 2}
    assert r["hashed_files"] == 12


def test_keyset_paging_no_gap_no_dup(env: sqlite3.Connection) -> None:
    """逐页翻完 7 组（limit=2）：不重、不漏、组数与一次性查询一致。"""
    one_shot = find_duplicates(env, SID, min_size=0, limit=1000)
    expect = [(it["hash_hex"], it["wasted_bytes"]) for it in one_shot["items"]]
    assert len(expect) == 4  # H1/H2/H3/H7；H4/H5/H6 独文件不成组

    seen: "list[tuple]" = []
    cursor = None
    pages = 0
    while True:
        r = find_duplicates(env, SID, min_size=0, limit=2, cursor=cursor)
        pages += 1
        seen += [(it["hash_hex"], it["wasted_bytes"]) for it in r["items"]]
        if not r["has_more"]:
            assert r["next_cursor"] is None
            break
        assert r["next_cursor"]
        cursor = r["next_cursor"]
    assert seen == expect
    assert pages == 2


def test_policy_not_full(tmp_path: Path) -> None:
    """meta.hash_policy 非 full → DuplicatesError（含 sampled / 未设置）。"""
    rows = [(1, 0, ".", "", 0, "dir", None, None, None, None, None, None),
            _row(2, "a1.bin", 2**20, "H1"), _row(3, "a2.bin", 2**20, "H1")]
    _build_snapshot(tmp_path, rows, FULL_META)
    db = tmp_path / "vol" / "20260101T000000Z" / "snapshot.db"
    ro = sqlite3.connect(f"file:{db.as_posix()}?mode=ro&immutable=1", uri=True)
    rw = sqlite3.connect(str(db))
    try:
        for policy in ("sampled", None):
            rw.execute("UPDATE meta SET value=? WHERE key='hash_policy'",
                       (policy,))
            rw.commit()
            with pytest.raises(DuplicatesError, match="policy full"):
                find_duplicates(ro, SID)
    finally:
        ro.close()
        rw.close()


def test_no_hashes_error(tmp_path: Path) -> None:
    """policy=full 但零哈希（模拟异常态）→ 无重复，不报错；policy none → 报错。"""
    rows = [(1, 0, ".", "", 0, "dir", None, None, None, None, None, None),
            _row(2, "x.bin", 999, "H1")]
    _build_snapshot(tmp_path, rows, {"status": "sealed", "hash_policy": "none"})
    db_path = tmp_path / "vol" / "20260101T000000Z" / "snapshot.db"
    conn = sqlite3.connect(
        f"file:{db_path.as_posix()}"
        "?mode=ro&immutable=1", uri=True)
    try:
        with pytest.raises(DuplicatesError):
            find_duplicates(conn, SID)
        w = sqlite3.connect(str(db_path))
        w.execute("UPDATE meta SET value='full' WHERE key='hash_policy'")
        w.commit()
        w.close()
        conn2 = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro&immutable=1",
                                uri=True)
        r = find_duplicates(conn2, SID, min_size=0)
        conn2.close()
        assert r["duplicate_groups"] == 0  # 单文件不成组
        assert r["total_wasted_bytes"] == 0
    finally:
        conn.close()


def test_paths_truncated_over_20(tmp_path: Path) -> None:
    """>20 个同哈希文件：只返回 20 条路径（按 path 排序），paths_truncated=true。"""
    rows = [(1, 0, ".", "", 0, "dir", None, None, None, None, None, None)]
    for i in range(25):
        rows.append(_row(2 + i, f"f{i:02d}.bin", 2**20, "HBIG"))
    _build_snapshot(tmp_path, rows, FULL_META)
    conn = sqlite3.connect(
        f"file:{(tmp_path / 'vol' / '20260101T000000Z' / 'snapshot.db').as_posix()}"
        "?mode=ro&immutable=1", uri=True)
    try:
        r = find_duplicates(conn, SID, min_size=0)
        it = r["items"][0]
        assert it["count"] == 25
        assert len(it["paths"]) == 20
        assert it["paths"] == sorted(it["paths"])
        assert it["paths"][0] == "f00.bin" and it["paths"][-1] == "f19.bin"
        assert it["paths_truncated"] is True
        assert r["total_wasted_bytes"] == 24 * 2**20
    finally:
        conn.close()


# ---------------------------------------------------------------- API

@pytest.fixture()
def client() -> TestClient:
    """路由级夹具：独立 data_root（2 组重复），完整走 create_app。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="cldm_dups_api_"))
    rows = [
        (1, 0, ".", "", 0, "dir", None, None, None, None, None, None),
        _row(2, "a1.bin", 2 * 2**20, "H1"),
        _row(3, "a2.bin", 2 * 2**20, "H1"),
        _row(4, "b1.bin", 4 * 2**20, "H2"),
        _row(5, "b2.bin", 4 * 2**20, "H2"),
    ]
    _build_snapshot(tmp, rows, FULL_META)
    app = create_app(str(tmp))
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_api_duplicates_ok(client: TestClient) -> None:
    resp = client.get(f"/api/snapshots/{SID}/duplicates")
    assert resp.status_code == 200
    r = resp.json()
    assert r["duplicate_groups"] == 2
    assert r["total_wasted_bytes"] == 4 * 2**20 + 2 * 2**20  # H2 4MiB + H1 2MiB
    assert {it["hash_hex"] for it in r["items"]} == {"H1", "H2"}
    assert r["items"][0]["hash_hex"] == "H2"  # wasted 相同按 size 降序


def test_api_unknown_snapshot_404(client: TestClient) -> None:
    assert client.get("/api/snapshots/nope/20260101T000000Z/duplicates").status_code == 404


def test_api_limit_over_max_400(client: TestClient) -> None:
    assert client.get(
        f"/api/snapshots/{SID}/duplicates",
        params={"limit": 1001}).status_code == 400
    assert client.get(
        f"/api/snapshots/{SID}/duplicates",
        params={"limit": 0}).status_code == 400


def test_api_bad_cursor_400(client: TestClient) -> None:
    resp = client.get(f"/api/snapshots/{SID}/duplicates", params={"cursor": "@@@bad"})
    assert resp.status_code == 400


def test_api_policy_error_400(client: TestClient) -> None:
    """policy=sampled → 400，detail 提示先跑 cldm hash --policy full。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="cldm_dups_sampled_"))
    rows = [(1, 0, ".", "", 0, "dir", None, None, None, None, None, None),
            _row(2, "a1.bin", 2 * 2**20, "H1"),
            _row(3, "a2.bin", 2 * 2**20, "H1")]
    _build_snapshot(tmp, rows, {"status": "sealed", "hash_policy": "sampled"})
    c = TestClient(create_app(str(tmp)))
    with c:
        resp = c.get(f"/api/snapshots/{SID}/duplicates")
    assert resp.status_code == 400
    assert "policy full" in resp.json()["detail"]


# ---------------------------------------------------------------- HTML 转义
def test_html_escapes_paths(tmp_path: Path) -> None:
    rows = [(1, 0, ".", "", 0, "dir", None, None, None, None, None, None),
            _row(2, "<script>alert(1)</script>.bin", 2**20, "H1"),
            _row(3, 'x&"y".bin', 2**20, "H1")]
    _build_snapshot(tmp_path, rows, FULL_META)
    conn = sqlite3.connect(
        f"file:{(tmp_path / 'vol' / '20260101T000000Z' / 'snapshot.db').as_posix()}"
        "?mode=ro&immutable=1", uri=True)
    try:
        r = find_duplicates(conn, SID, min_size=0)
        page = render_duplicates_html(r)
        assert "<script>" not in page
        assert "&lt;script&gt;" in page
        assert "<code>" in page  # 自身标签正常
    finally:
        conn.close()


# ---------------------------------------------------------------- CLI

def _cli_env(tmp_path: Path) -> Path:
    rows = [(1, 0, ".", "", 0, "dir", None, None, None, None, None, None),
            _row(2, "a1.bin", 2**20, "H1"),
            _row(3, "a2.bin", 2**20, "H1")]
    _build_snapshot(tmp_path, rows, FULL_META)
    return tmp_path


def test_cli_text_and_csv(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from cold_manifest.cli import main
    data = _cli_env(tmp_path)
    rc = main(["duplicates", SID, "--data-root", str(data)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "重复组数：1" in out and "可回收空间" in out and "a1.bin" in out

    rc = main(["duplicates", SID, "--data-root", str(data), "--output", "csv"])
    assert rc == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("hash_hex,size_bytes")
    assert lines[1].startswith("H1,")


def test_cli_html_and_error(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    from cold_manifest.cli import main
    data = _cli_env(tmp_path)
    html_path = tmp_path / "rep.html"
    rc = main(["duplicates", SID, "--data-root", str(data),
               "--html", str(html_path), "--min-size", "0"])
    assert rc == 0 and html_path.is_file()
    page = html_path.read_text(encoding="utf-8")
    assert "重复文件报告" in page and "可回收空间" in page
    capsys.readouterr()

    # 非 full 策略 → 退出码 2
    db = data / "vol" / "20260101T000000Z" / "snapshot.db"
    w = sqlite3.connect(str(db))
    w.execute("UPDATE meta SET value='sampled' WHERE key='hash_policy'")
    w.commit(); w.close()
    rc = main(["duplicates", SID, "--data-root", str(data)])
    assert rc == 2
    assert "policy full" in capsys.readouterr().err

    # 不存在的快照 → 退出码 2
    assert main(["duplicates", "nope/20260101T000000Z",
                 "--data-root", str(data)]) == 2
