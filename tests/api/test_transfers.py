"""Tests for transfer detection, confirmation and the Transfers dashboard page.

Query-layer tests run on ``db_session``; HTTP tests use ``auth_client`` (both
share the same temp engine, so writes are visible across them).
"""

from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from expense_analyzer.models import (
    Account,
    AccountType,
    Category,
    CategoryKind,
    ImportBatch,
    Transaction,
)
from expense_analyzer.queries import transfers as tq


def _account(session: Session, name: str) -> Account:
    acc = Account(name=name, type=AccountType.bank)
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return acc


def _batch(session: Session) -> ImportBatch:
    batch = ImportBatch(source="test", filename="test.csv")
    session.add(batch)
    session.commit()
    session.refresh(batch)
    return batch


def _tx(session: Session, *, account_id: int, amount: int, day: int) -> Transaction:
    tx = Transaction(
        account_id=account_id,
        import_batch_id=_batch(session).id,
        amount=amount,
        booked_date=date(2026, 5, day),
        raw_description=f"row {account_id}/{amount}/{day}",
        fingerprint=f"fp-{account_id}-{amount}-{day}",
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)
    return tx


def test_link_transfer_marks_both_legs_and_assigns_category(db_session: Session):
    a = _account(db_session, "PKO")
    b = _account(db_session, "mBank")
    out = _tx(db_session, account_id=a.id, amount=-200000, day=1)
    inn = _tx(db_session, account_id=b.id, amount=200000, day=2)

    group_id = tq.link_transfer(db_session, tx_a_id=out.id, tx_b_id=inn.id)
    assert group_id is not None

    transfer_cat = db_session.exec(
        select(Category).where(Category.kind == CategoryKind.transfer)
    ).one()
    db_session.refresh(out)
    db_session.refresh(inn)
    assert out.transfer_group_id == group_id == inn.transfer_group_id
    assert out.category_id == transfer_cat.id == inn.category_id


def test_link_transfer_rejects_invalid_pairs(db_session: Session):
    a = _account(db_session, "PKO")
    b = _account(db_session, "mBank")
    out = _tx(db_session, account_id=a.id, amount=-200000, day=1)
    same_sign = _tx(db_session, account_id=b.id, amount=-200000, day=1)
    same_acct = _tx(db_session, account_id=a.id, amount=200000, day=1)
    unequal = _tx(db_session, account_id=b.id, amount=199999, day=1)

    assert tq.link_transfer(db_session, tx_a_id=out.id, tx_b_id=same_sign.id) is None
    assert tq.link_transfer(db_session, tx_a_id=out.id, tx_b_id=same_acct.id) is None
    assert tq.link_transfer(db_session, tx_a_id=out.id, tx_b_id=unequal.id) is None
    assert tq.link_transfer(db_session, tx_a_id=out.id, tx_b_id=999999) is None


def test_detect_and_autolink_links_unambiguous(db_session: Session):
    a = _account(db_session, "PKO")
    b = _account(db_session, "mBank")
    _tx(db_session, account_id=a.id, amount=-200000, day=1)
    _tx(db_session, account_id=b.id, amount=200000, day=2)

    linked, result = tq.detect_and_autolink(db_session, window_days=3)
    assert linked == 1
    assert not result.ambiguous
    # Both legs now matched -> no remaining candidates.
    assert tq.unmatched_candidates(db_session) == []


def test_unlink_clears_group_and_transfer_category(db_session: Session):
    a = _account(db_session, "PKO")
    b = _account(db_session, "mBank")
    out = _tx(db_session, account_id=a.id, amount=-200000, day=1)
    inn = _tx(db_session, account_id=b.id, amount=200000, day=2)
    group_id = tq.link_transfer(db_session, tx_a_id=out.id, tx_b_id=inn.id)

    assert tq.unlink_transfer(db_session, group_id) == 2

    db_session.refresh(out)
    db_session.refresh(inn)
    assert out.transfer_group_id is None and inn.transfer_group_id is None
    assert out.category_id is None and inn.category_id is None


def test_unlink_keeps_a_manually_recategorized_leg(db_session: Session):
    # After auto/confirm, a user re-tags one leg to a real category. Unlinking the
    # transfer must clear the group on both legs but NOT clobber that manual category.
    a = _account(db_session, "PKO")
    b = _account(db_session, "mBank")
    out = _tx(db_session, account_id=a.id, amount=-200000, day=1)
    inn = _tx(db_session, account_id=b.id, amount=200000, day=2)
    group_id = tq.link_transfer(db_session, tx_a_id=out.id, tx_b_id=inn.id)

    groceries = Category(name="Groceries", kind=CategoryKind.expense)
    db_session.add(groceries)
    db_session.commit()
    db_session.refresh(groceries)
    out.category_id = groceries.id  # human re-tags one leg
    db_session.add(out)
    db_session.commit()

    tq.unlink_transfer(db_session, group_id)

    db_session.refresh(out)
    db_session.refresh(inn)
    assert out.transfer_group_id is None and inn.transfer_group_id is None
    assert out.category_id == groceries.id  # manual category preserved
    assert inn.category_id is None  # the Transfer category was cleared


def test_transfers_page_renders(auth_client: TestClient, db_session: Session):
    resp = auth_client.get("/dashboard/transfers")
    assert resp.status_code == 200
    assert "Transfers" in resp.text


def test_confirm_route_links_pair(auth_client: TestClient, db_session: Session):
    a = _account(db_session, "PKO")
    b = _account(db_session, "mBank")
    out = _tx(db_session, account_id=a.id, amount=-200000, day=1)
    inn = _tx(db_session, account_id=b.id, amount=200000, day=2)

    resp = auth_client.post(
        "/dashboard/transfers/confirm",
        data={"tx_a_id": out.id, "tx_b_id": inn.id},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db_session.refresh(out)
    db_session.refresh(inn)
    assert out.transfer_group_id is not None
    assert out.transfer_group_id == inn.transfer_group_id


def test_confirm_route_404_on_invalid_pair(auth_client: TestClient, db_session: Session):
    a = _account(db_session, "PKO")
    out = _tx(db_session, account_id=a.id, amount=-200000, day=1)
    same_acct = _tx(db_session, account_id=a.id, amount=200000, day=1)

    resp = auth_client.post(
        "/dashboard/transfers/confirm",
        data={"tx_a_id": out.id, "tx_b_id": same_acct.id},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_rescan_route_autolinks(auth_client: TestClient, db_session: Session):
    a = _account(db_session, "PKO")
    b = _account(db_session, "mBank")
    out = _tx(db_session, account_id=a.id, amount=-200000, day=1)
    inn = _tx(db_session, account_id=b.id, amount=200000, day=2)

    resp = auth_client.post("/dashboard/transfers/rescan", follow_redirects=False)
    assert resp.status_code == 303

    db_session.refresh(out)
    db_session.refresh(inn)
    assert out.transfer_group_id is not None
    assert out.transfer_group_id == inn.transfer_group_id
