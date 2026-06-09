"""Home Assistant page (Phase 7) — endpoint smoke + the manual publish button."""

from collections.abc import Callable

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlmodel import Session

from expense_analyzer.api.endpoints.core import home_assistant
from expense_analyzer.config import get_settings
from expense_analyzer.models import Account, Transaction


def test_page_off_by_default_shows_how_to_enable(auth_client: TestClient) -> None:
    resp = auth_client.get("/dashboard/home-assistant")
    assert resp.status_code == status.HTTP_200_OK
    assert "EA_MQTT_HOST" in resp.text  # the off-state hint


def test_page_previews_the_metrics(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
) -> None:
    account = make_account(name="PKO checking")
    make_transaction(account_id=account.id, amount=-100_00)

    resp = auth_client.get("/dashboard/home-assistant")
    assert resp.status_code == status.HTTP_200_OK
    assert "PKO checking Balance" in resp.text  # the per-account sensor preview
    assert "net_worth" in resp.text


def test_publish_without_config_flashes_error(auth_client: TestClient) -> None:
    resp = auth_client.post("/dashboard/home-assistant/publish")
    # MQTT is not configured in tests -> a clear flash, not a 500.
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "not configured" in resp.text.lower()


@pytest.fixture
def mqtt_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure an MQTT host (so the publish path is reachable) without a broker."""
    monkeypatch.setenv("EA_MQTT_HOST", "broker.local")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_publish_now_pushes_and_reports_count(
    auth_client: TestClient,
    mqtt_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Stub the actual push so the routing/flash is tested without a broker
    # (the publisher itself is covered by tests/ha/test_mqtt.py).
    monkeypatch.setattr(home_assistant, "publish_snapshot", lambda session, settings: 4)

    resp = auth_client.post("/dashboard/home-assistant/publish")
    assert resp.status_code == status.HTTP_200_OK
    assert "Published 4 sensors" in resp.text
