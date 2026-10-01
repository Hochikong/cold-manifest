"""Windows 兼容修复 A：只读文件删除（rmtree_ro）+ 连接池释放后重试删除。"""

import sqlite3
import stat

import pytest
from fastapi.testclient import TestClient

from cold_manifest.api.state import AppState
from cold_manifest.catalog import (
    catalog_path,
    ensure_disk,
    ensure_volume,
    register_snapshot,
    rmtree_ro,
)
from cold_manifest.db import init_catalog, init_snapshot
from cold_manifest.server import create_app

from test_delete_snapshot import SID, TS, Env, _build_snapshot_db


# ---------------------------------------------------------------- rmtree_ro


def test_rmtree_ro_deletes_readonly_file(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    ro = d / "ro.txt"
    ro.write_text("x")
    ro.chmod(0o444)
    (d / "sub").mkdir()
    (d / "sub" / "ro2.bin").write_bytes(b"y")
    (d / "sub" / "ro2.bin").chmod(0o444)

    rmtree_ro(d)
    assert not d.exists()


def test_rmtree_ro_missing_dir_is_silent(tmp_path):
    rmtree_ro(tmp_path / "no" / "such" / "dir")  # 不抛错


# ---------------------------------------------------------------- AppState.evict_snapshot


def test_evict_snapshot_closes_only_target(tmp_path):
    sid2 = "vol/20260102T000000Z"
    _build_snapshot_db(tmp_path / "vol" / TS / "snapshot.db")
    _build_snapshot_db(tmp_path / "vol" / "20260102T000000Z" / "snapshot.db")

    st = AppState(tmp_path)
    c1 = st.snapshot_db(SID)
    c2 = st.snapshot_db(sid2)

    # retired 里也放一个属于 SID 的连接（模拟曾被 LRU 挤出；换新连接确保不同对象）
    del st._snap_pools[SID]
    c1_old = st.snapshot_db(SID)
    del st._snap_pools[SID]  # 从池里摘出，仅留在 retired（模拟曾被挤出）
    st._retired.append((SID, c1_old))
    assert c1_old is not c1

    assert st.evict_snapshot(SID) == 1  # retired 里那条
    with pytest.raises(sqlite3.ProgrammingError):
        c1_old.execute("SELECT 1")
    # 池外的旧引用与另一个快照不受影响
    c1.execute("SELECT 1")
    c2.execute("SELECT 1")
    assert st._snap_pools[sid2] is c2

    # 池内路径：再取一次进池，evict 后应关闭且从池里移除
    c1b = st.snapshot_db(SID)
    assert st.evict_snapshot(SID) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        c1b.execute("SELECT 1")
    assert SID not in st._snap_pools

    st.close()


# ---------------------------------------------------------------- DELETE 路由：先 evict 再重试


def test_delete_retries_after_evict(monkeypatch, tmp_path):
    env = Env(tmp_path)
    real_rmtree = rmtree_ro
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(13, "被占用", str(path))
        real_rmtree(path)

    import cold_manifest.catalog as cat_mod
    monkeypatch.setattr(cat_mod, "rmtree_ro", flaky)

    client = env.client()
    with client:
        st = client.app.state.cldm
        orig_evict = type(st).evict_snapshot
        evicts = []

        def spy_evict(self, sid):
            evicts.append(sid)
            return orig_evict(self, sid)

        monkeypatch.setattr(type(st), "evict_snapshot", spy_evict)

        resp = client.delete(f"/api/snapshots/{SID}")
    assert resp.status_code == 200, resp.text
    assert calls["n"] == 2  # 第一次失败、第二次成功
    assert evicts == [SID, SID]  # 删除前 evict + 失败后再次 evict
    assert not env.host_dir.exists()


def test_delete_busy_raises_readable_error(monkeypatch, tmp_path):
    env = Env(tmp_path)

    def always_busy(path):
        raise PermissionError(13, "被占用", str(path))

    import cold_manifest.catalog as cat_mod
    monkeypatch.setattr(cat_mod, "rmtree_ro", always_busy)

    client = env.client()
    with client:
        resp = client.delete(f"/api/snapshots/{SID}")
    assert resp.status_code == 409
    assert "占用" in resp.json()["detail"]
    assert "只读" in resp.json()["detail"]
    # 未谎报成功：目录还在，catalog 行未删
    assert env.host_dir.exists()
    cat = sqlite3.connect(catalog_path(env.data_root))
    assert cat.execute(
        "SELECT COUNT(*) FROM snapshots WHERE snapshot_id=?", (SID,)).fetchone()[0] == 1
    cat.close()


# ---------------------------------------------------------------- 真实小链路：查询后 DELETE


def test_query_then_delete_releases_connection(tmp_path):
    env = Env(tmp_path)
    client = env.client()
    with client:
        # 走一次查询端点，把快照库只读连接放进池
        r = client.get(f"/api/snapshots/{SID}/stats")
        assert r.status_code == 200, r.text
        assert client.app.state.cldm._snap_pools.get(SID) is not None

        resp = client.delete(f"/api/snapshots/{SID}")
        assert resp.status_code == 200, resp.text
    assert not env.host_dir.exists()
