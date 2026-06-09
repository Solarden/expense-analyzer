"""Categorization rules: query layer, Rules page, and the import-time hook
(Phase 10, design §7.7).

HTTP tests use ``auth_client`` (logged in), sharing the temp engine with
``db_session`` so a rule created over HTTP is visible to a query-layer assertion.
Bad input must come back as a 400 re-render with a flash, never a 500.
"""

from collections.abc import Callable
from datetime import date

from fastapi import status
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from expense_analyzer.importers import NormalizedTransaction
from expense_analyzer.importers.pipeline import run_import
from expense_analyzer.models import (
    Account,
    Category,
    CategoryKind,
    Transaction,
    TxSource,
)
from expense_analyzer.queries.categorize import rules as rq

# --- query layer ----------------------------------------------------------


def test_create_and_list_rules_in_evaluation_order(
    db_session: Session, make_category: Callable[..., Category]
) -> None:
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    # Created low-priority first, then high — list must come back high-priority first.
    rq.create_rule(db_session, pattern="A", category_id=food.id, priority=0)
    rq.create_rule(db_session, pattern="B", category_id=fun.id, priority=10)

    patterns = [r.pattern for r in rq.list_rules(db_session)]
    assert patterns == ["B", "A"]


def test_delete_rule(db_session: Session, make_category: Callable[..., Category]) -> None:
    cat = make_category()
    rule = rq.create_rule(db_session, pattern="X", category_id=cat.id)

    assert rq.delete_rule(db_session, rule.id) is True
    assert rq.delete_rule(db_session, rule.id) is False
    assert rq.list_rules(db_session) == []


def test_apply_rules_fills_uncategorized(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    tx = make_transaction(account_id=account.id, amount=-5000, merchant_normalized="BIEDRONKA 12")
    rq.create_rule(db_session, pattern="BIEDRONKA", category_id=food.id)

    assert rq.apply_rules(db_session) == 1
    db_session.refresh(tx)
    assert tx.category_id == food.id
    assert tx.source == TxSource.rule
    assert tx.confidence == 1.0


def test_apply_rules_never_overwrites_manual(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    other = make_category(name="Other")
    tx = make_transaction(
        account_id=account.id,
        amount=-5000,
        merchant_normalized="BIEDRONKA",
        category_id=other.id,
        source=TxSource.manual,
    )
    rq.create_rule(db_session, pattern="BIEDRONKA", category_id=food.id)

    assert rq.apply_rules(db_session) == 0  # manual row is off-limits
    db_session.refresh(tx)
    assert tx.category_id == other.id
    assert tx.source == TxSource.manual


def test_apply_rules_never_recategorizes_manually_cleared(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    # A human deliberately cleared the category (category_id=None, source=manual).
    # A matching rule must NOT re-categorize it — keying on source, not just null.
    food = make_category(name="Food")
    tx = make_transaction(
        account_id=account.id,
        amount=-5000,
        merchant_normalized="BIEDRONKA",
        category_id=None,
        source=TxSource.manual,
    )
    rq.create_rule(db_session, pattern="BIEDRONKA", category_id=food.id)

    assert rq.apply_rules(db_session) == 0
    db_session.refresh(tx)
    assert tx.category_id is None
    assert tx.source == TxSource.manual


def test_apply_rules_leaves_categorized_transfer_leg(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    # A linked transfer leg has the Transfer category with source=import_csv. It
    # must stay put even if a rule matches its description.
    from expense_analyzer.models import CategoryKind

    transfer = make_category(name="Transfer", kind=CategoryKind.transfer)
    food = make_category(name="Food")
    tx = make_transaction(
        account_id=account.id,
        amount=-5000,
        merchant_normalized="BIEDRONKA",
        category_id=transfer.id,
        source=TxSource.import_csv,
        transfer_group_id="grp1",
    )
    rq.create_rule(db_session, pattern="BIEDRONKA", category_id=food.id)

    assert rq.apply_rules(db_session) == 0
    db_session.refresh(tx)
    assert tx.category_id == transfer.id


def test_apply_rules_re_categorizes_previous_rule_rows(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    old = make_category(name="Old")
    new = make_category(name="New")
    tx = make_transaction(
        account_id=account.id,
        amount=-5000,
        merchant_normalized="BIEDRONKA",
        category_id=old.id,
        source=TxSource.rule,  # was set by a (since changed) rule
    )
    rq.create_rule(db_session, pattern="BIEDRONKA", category_id=new.id)

    assert rq.apply_rules(db_session) == 1
    db_session.refresh(tx)
    assert tx.category_id == new.id


def test_apply_rules_uses_raw_description_fallback(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    cat = make_category(name="Cash")
    tx = make_transaction(
        account_id=account.id,
        amount=-2000,
        raw_description="ATM WITHDRAWAL ZABKA",
        merchant_normalized=None,
    )
    rq.create_rule(db_session, pattern="ZABKA", category_id=cat.id)

    assert rq.apply_rules(db_session) == 1
    db_session.refresh(tx)
    assert tx.category_id == cat.id


def test_apply_rules_no_rules_is_zero(db_session: Session, account: Account) -> None:
    assert rq.apply_rules(db_session) == 0


def test_apply_rules_skips_already_correct(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    make_transaction(
        account_id=account.id,
        amount=-5000,
        merchant_normalized="BIEDRONKA",
        category_id=food.id,
        source=TxSource.rule,
    )
    rq.create_rule(db_session, pattern="BIEDRONKA", category_id=food.id)

    assert rq.apply_rules(db_session) == 0  # already in the matched category


# --- import-time hook -----------------------------------------------------


def test_import_auto_categorizes_new_rows(
    db_session: Session, account: Account, make_category: Callable[..., Category]
) -> None:
    food = make_category(name="Food")
    rq.create_rule(db_session, pattern="BIEDRONKA", category_id=food.id)

    from tests.conftest import FakeImporter

    importer = FakeImporter(
        [
            NormalizedTransaction(date(2026, 5, 1), -5000, "BIEDRONKA 99 WARSZAWA"),
            NormalizedTransaction(date(2026, 5, 2), -1234, "SOME OTHER SHOP"),
        ]
    )
    summary = run_import(
        db_session, account_id=account.id, importer=importer, filename="x.csv", data=b""
    )

    assert summary.new == 2
    assert summary.auto_categorized == 1  # only the Biedronka row matched
    rows = db_session.exec(select(Transaction).where(Transaction.category_id == food.id)).all()
    assert len(rows) == 1


# --- endpoints ------------------------------------------------------------


def test_rules_page_renders(auth_client: TestClient) -> None:
    resp = auth_client.get("/dashboard/rules")
    assert resp.status_code == status.HTTP_200_OK
    assert "Categorization rules" in resp.text


def test_create_rule_over_http(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
) -> None:
    cat = make_category(name="Food", kind=CategoryKind.expense)

    resp = auth_client.post(
        "/dashboard/rules",
        data={"pattern": "BIEDRONKA", "category_id": cat.id, "priority": "5"},
        follow_redirects=False,
    )

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    [rule] = rq.list_rules(db_session)
    assert rule.pattern == "BIEDRONKA"
    assert rule.priority == 5


def test_create_rule_rejects_blank_pattern(
    auth_client: TestClient, make_category: Callable[..., Category]
) -> None:
    cat = make_category(name="Food")

    resp = auth_client.post(
        "/dashboard/rules", data={"pattern": "   ", "category_id": cat.id, "priority": "0"}
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "can&#39;t be empty" in resp.text or "empty" in resp.text


def test_create_rule_rejects_transfer_category(
    auth_client: TestClient, make_category: Callable[..., Category]
) -> None:
    transfer = make_category(name="Transfer", kind=CategoryKind.transfer)

    resp = auth_client.post(
        "/dashboard/rules",
        data={"pattern": "X", "category_id": transfer.id, "priority": "0"},
    )

    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "expense or income" in resp.text


def test_delete_rule_over_http(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
) -> None:
    cat = make_category()
    rule = rq.create_rule(db_session, pattern="X", category_id=cat.id)

    resp = auth_client.post(f"/dashboard/rules/{rule.id}/delete", follow_redirects=False)

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert rq.list_rules(db_session) == []


def test_apply_now_endpoint_reports_count(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    make_transaction(account_id=account.id, amount=-5000, merchant_normalized="BIEDRONKA")
    rq.create_rule(db_session, pattern="BIEDRONKA", category_id=food.id)

    resp = auth_client.post("/dashboard/rules/apply", follow_redirects=False)

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"] == "/dashboard/rules?applied=1"
    # The follow-up GET renders the result flash.
    page = auth_client.get("/dashboard/rules?applied=1")
    assert "1 transaction categorized" in page.text


def test_prefill_pattern_in_form(
    auth_client: TestClient, make_category: Callable[..., Category]
) -> None:
    make_category(name="Food")  # the create form only renders when a category exists
    resp = auth_client.get("/dashboard/rules?pattern=BIEDRONKA")
    assert 'value="BIEDRONKA"' in resp.text
