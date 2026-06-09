"""The in-app Updates view (/dashboard/updates) — read-only render of the verdict
the cron check persisted. The page itself never touches the network."""

import json

from fastapi import status as http
from fastapi.testclient import TestClient

from expense_analyzer.clock import utc_now
from expense_analyzer.config import get_settings


def _write_status(**fields) -> None:
    path = get_settings().update_status_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"checked_at": utc_now().isoformat(), **fields}
    path.write_text(json.dumps(payload))


def _clear_status() -> None:
    path = get_settings().update_status_path
    path.unlink(missing_ok=True)


def test_no_check_yet(auth_client: TestClient):
    _clear_status()
    resp = auth_client.get("/dashboard/updates")
    assert resp.status_code == http.HTTP_200_OK
    assert "No update check has run yet" in resp.text


def test_update_available_nudges_to_deploy(auth_client: TestClient):
    _write_status(current="v1.2.0", latest="v1.3.0", update_available=True)
    try:
        resp = auth_client.get("/dashboard/updates")
        assert resp.status_code == http.HTTP_200_OK
        body = resp.text
        assert "new version is available" in body
        assert "v1.3.0" in body
        assert "make deploy" in body  # notify-only nudge, not an in-app deploy button
        # Changelog is a plain outbound link to the release; the app fetches nothing.
        assert "/releases/tag/v1.3.0" in body
    finally:
        _clear_status()


def test_up_to_date(auth_client: TestClient):
    _write_status(current="v1.3.0", latest="v1.3.0", update_available=False)
    try:
        resp = auth_client.get("/dashboard/updates")
        assert resp.status_code == http.HTTP_200_OK
        assert "latest release" in resp.text.lower()
    finally:
        _clear_status()
