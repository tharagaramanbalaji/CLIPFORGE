from __future__ import annotations

from fastapi.testclient import TestClient

from clipforge.api.deps import readiness_probes
from clipforge.api.main import create_app


def _client(probes: dict[str, object]) -> TestClient:
    app = create_app()
    app.dependency_overrides[readiness_probes] = lambda: probes
    return TestClient(app)


def test_health() -> None:
    r = _client({}).get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ready_all_ok() -> None:
    r = _client({"database": lambda: None, "redis": lambda: None}).get("/api/v1/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}


def test_ready_failure_hides_details() -> None:
    def broken() -> None:
        raise ConnectionError("password=hunter2 host=db")

    r = _client({"database": broken, "redis": lambda: None}).get("/api/v1/ready")
    assert r.status_code == 503
    assert r.json()["checks"]["database"] == "fail"
    assert "hunter2" not in r.text
