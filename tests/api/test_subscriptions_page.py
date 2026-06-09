"""Subscriptions dashboard page + query layer (Phase 9).

Detection is pure (covered in tests/unit/test_subscriptions.py); here we check the
DB wiring — that spendable transactions feed the detector, that confirm/dismiss/
restore verdicts persist, and that dismissed groups drop out of the monthly total.

HTTP tests use ``auth_client`` (logged in); it shares the temp engine with
``db_session`` so a verdict set over HTTP is visible to a query-layer assertion.
The file is ``..._page.py`` because test module names must be globally unique
(no ``__init__.py``) and a unit test already owns ``test_subscriptions.py``.
"""

from collections.abc import Callable
from datetime import date

from fastapi import status
from fastapi.testclient import TestClient
from sqlmodel import Session

from expense_analyzer.config import Settings
from expense_analyzer.models import Account, SubscriptionStatus, Transaction
from expense_analyzer.queries.planning import subscriptions as sq

SETTINGS = Settings(secret_key="test-secret-not-for-production")


def _seed_monthly(
    make_transaction: Callable[..., Transaction],
    account_id: int,
    *,
    merchant: str = "NETFLIX",
    amount: int = -2999,
) -> None:
    """Three monthly charges ending mid-May 2026 (active as of 2026-06-04)."""
    for month in (3, 4, 5):
        make_transaction(
            account_id=account_id,
            amount=amount,
            booked_date=date(2026, month, 15),
            merchant_normalized=merchant,
        )


def test_overview_detects_subscription(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
) -> None:
    _seed_monthly(make_transaction, account.id)

    views = sq.subscription_overview(db_session, SETTINGS, today=date(2026, 6, 4))

    assert len(views) == 1
    assert views[0].detected.merchant == "NETFLIX"
    assert views[0].verdict is None  # no verdict stored yet -> a suggestion


def test_set_and_clear_verdict(db_session: Session) -> None:
    sq.set_verdict(db_session, merchant="NETFLIX", status=SubscriptionStatus.confirmed)
    assert sq.list_verdicts(db_session) == {"NETFLIX": SubscriptionStatus.confirmed}

    # Upsert: re-setting updates in place, never duplicates.
    sq.set_verdict(db_session, merchant="NETFLIX", status=SubscriptionStatus.dismissed)
    assert sq.list_verdicts(db_session) == {"NETFLIX": SubscriptionStatus.dismissed}

    assert sq.clear_verdict(db_session, "NETFLIX") is True
    assert sq.list_verdicts(db_session) == {}
    assert sq.clear_verdict(db_session, "NETFLIX") is False  # nothing left to clear


def test_active_monthly_cost_excludes_dismissed(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
) -> None:
    _seed_monthly(make_transaction, account.id, merchant="NETFLIX", amount=-2999)
    _seed_monthly(make_transaction, account.id, merchant="SPOTIFY", amount=-1999)

    today = date(2026, 6, 4)
    assert (
        sq.active_monthly_cost(sq.subscription_overview(db_session, SETTINGS, today=today)) == 4998
    )

    sq.set_verdict(db_session, merchant="SPOTIFY", status=SubscriptionStatus.dismissed)
    assert (
        sq.active_monthly_cost(sq.subscription_overview(db_session, SETTINGS, today=today)) == 2999
    )


def test_page_renders(auth_client: TestClient) -> None:
    resp = auth_client.get("/dashboard/subscriptions")

    assert resp.status_code == status.HTTP_200_OK
    assert "Subscriptions" in resp.text
    assert "fixed monthly costs" in resp.text


def test_confirm_then_dismiss_then_restore(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
) -> None:
    _seed_monthly(make_transaction, account.id)

    resp = auth_client.post(
        "/dashboard/subscriptions/confirm", data={"merchant": "NETFLIX"}, follow_redirects=False
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert sq.list_verdicts(db_session) == {"NETFLIX": SubscriptionStatus.confirmed}

    auth_client.post("/dashboard/subscriptions/dismiss", data={"merchant": "NETFLIX"})
    assert sq.list_verdicts(db_session) == {"NETFLIX": SubscriptionStatus.dismissed}

    auth_client.post("/dashboard/subscriptions/restore", data={"merchant": "NETFLIX"})
    assert sq.list_verdicts(db_session) == {}


def test_dismissed_subscription_hidden_from_suggestions(
    auth_client: TestClient,
    account: Account,
    make_transaction: Callable[..., Transaction],
) -> None:
    _seed_monthly(make_transaction, account.id)
    auth_client.post("/dashboard/subscriptions/dismiss", data={"merchant": "NETFLIX"})

    resp = auth_client.get("/dashboard/subscriptions")

    # Still on the page (in the Dismissed panel), but flagged as dismissed.
    assert "NETFLIX" in resp.text
    assert "Dismissed" in resp.text
    assert "Restore" in resp.text
