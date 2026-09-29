"""健康检查端点测试。"""

from fastapi.testclient import TestClient

from cold_manifest import __version__
from cold_manifest.server import app


def test_health() -> None:
    client = TestClient(app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": __version__}
