from fastapi import status
from fastapi.testclient import TestClient


def test_root(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["app"]
    assert "version" in body


def test_health_reports_ok_and_wal(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    # db.py forces WAL on every connection.
    assert body["journal_mode"].lower() == "wal"
