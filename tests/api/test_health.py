from fastapi import status
from fastapi.testclient import TestClient


def test_root_redirects_to_dashboard(client: TestClient):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == status.HTTP_307_TEMPORARY_REDIRECT
    assert resp.headers["location"] == "/dashboard"


def test_health_reports_ok_and_dialect(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    # Whatever engine the suite runs on, the dialect is reported for diagnostics.
    assert body["dialect"] in {"sqlite", "postgresql"}


def test_health_returns_503_when_database_unreachable(client: TestClient, monkeypatch):
    # The docker healthcheck only looks at the status code, so a DB outage must
    # surface as a non-200 — not a 200 that happens to say "degraded".
    from sqlalchemy import create_engine

    from expense_analyzer.api.endpoints.core import health as health_mod

    # Port 1 on localhost: connection refused immediately, no timeout to wait out.
    dead_engine = create_engine("postgresql+psycopg://x:y@127.0.0.1:1/nope")
    monkeypatch.setattr(health_mod, "get_engine", lambda: dead_engine)

    resp = client.get("/health")

    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["database"] == "unreachable"
