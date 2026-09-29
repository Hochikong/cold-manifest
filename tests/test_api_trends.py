"""多代趋势 API 测试：GET /api/volumes/{volume_id}/trends（P2-C）。"""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cold_manifest.catalog import catalog_path, ensure_disk, ensure_volume, register_snapshot
from cold_manifest.db import init_catalog
from cold_manifest.server import create_app


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    cat = sqlite3.connect(catalog_path(root))
    init_catalog(cat)
    ensure_disk(cat, "DISK1", physical_model="TOSHIBA", capacity_bytes=2000)
    ensure_volume(cat, "vol", "DISK1", filesystem="NTFS")
    cat.commit()
    cat.close()
    return root


@pytest.fixture()
def cat(data_root: Path):
    conn = sqlite3.connect(catalog_path(data_root))
    yield conn
    conn.close()


@pytest.fixture()
def client(data_root: Path) -> TestClient:
    with TestClient(create_app(data_root=str(data_root))) as c:
        yield c


def _add(cat, sid: str, ts: str, status: str = "sealed", **stats) -> None:
    register_snapshot(cat, sid, "vol", status=status, collected_at=ts, host_path="/t",
                      hash_policy="none", zero_byte_count=0, **stats)
    cat.commit()


BASE = "/api/volumes/vol/trends"


def test_empty_volume(client: TestClient) -> None:
    r = client.get(BASE)
    assert r.status_code == 200
    assert r.json() == {"volume_id": "vol", "items": []}


def test_single_snapshot(client: TestClient, cat) -> None:
    _add(cat, "vol/20260101T000000Z", "2026-01-01T00:00:00Z",
         file_count=4, dir_count=2, total_bytes=907000, max_depth=3)
    body = client.get(BASE).json()
    assert body["volume_id"] == "vol"
    assert body["items"] == [{
        "snapshot_id": "vol/20260101T000000Z",
        "collect_time": "2026-01-01T00:00:00Z",
        "status": "sealed",
        "file_count": 4,
        "dir_count": 2,
        "total_bytes": 907000,
        "max_depth": 3,
    }]


def test_multiple_sorted_ascending(client: TestClient, cat) -> None:
    for i, ts in enumerate(["2026-03-01T00:00:00Z", "2026-01-01T00:00:00Z",
                            "2026-02-01T00:00:00Z"], start=1):
        _add(cat, f"vol/20260{i}01T000000Z", ts, file_count=i * 10, dir_count=1,
             total_bytes=i * 100, max_depth=i)
    times = [it["collect_time"] for it in client.get(BASE).json()["items"]]
    assert times == sorted(times)
    assert len(times) == 3


def test_only_sealed(client: TestClient, cat) -> None:
    _add(cat, "vol/a", "2026-01-01T00:00:00Z", file_count=1, dir_count=0,
         total_bytes=1, max_depth=1)
    _add(cat, "vol/b", "2026-01-02T00:00:00Z", status="collecting", file_count=9,
         dir_count=0, total_bytes=9, max_depth=9)
    items = client.get(BASE).json()["items"]
    assert [it["snapshot_id"] for it in items] == ["vol/a"]


def test_limit_default_and_values(client: TestClient, cat) -> None:
    for i in range(150):
        _add(cat, f"vol/{i:04d}", f"2026-01-01T00:{i:02d}:00Z", file_count=i,
             dir_count=0, total_bytes=i, max_depth=1)
    # 默认 100
    assert len(client.get(BASE).json()["items"]) == 100
    # 显式覆盖
    assert len(client.get(BASE, params={"limit": 10}).json()["items"]) == 10
    # 升序：取最早 10 条
    times = [it["collect_time"] for it in client.get(BASE, params={"limit": 10}).json()["items"]]
    assert times == sorted(times)
    # 上限 1000 可用
    assert client.get(BASE, params={"limit": 1000}).status_code == 200


@pytest.mark.parametrize("limit", [0, 1001, -1])
def test_limit_invalid_400(client: TestClient, limit: int) -> None:
    assert client.get(BASE, params={"limit": limit}).status_code == 400


def test_unknown_volume_404(client: TestClient) -> None:
    assert client.get("/api/volumes/nope/trends").status_code == 404


@pytest.mark.parametrize("vid", [".hidden", "-lead", "a b", "sp%20ace"])
def test_invalid_volume_id_400(client: TestClient, vid: str) -> None:
    assert client.get(f"/api/volumes/{vid}/trends").status_code == 400


@pytest.mark.parametrize("vid", ["../etc", "a/b", ""])
def test_traversal_paths_do_not_reach_handler(client: TestClient, vid: str) -> None:
    # 含路径分隔符/穿越片段的 id 在路由层即被挡下（404），不会触达处理器
    assert client.get(f"/api/volumes/{vid}/trends").status_code == 404
