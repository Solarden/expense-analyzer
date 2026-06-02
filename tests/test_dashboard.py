"""HTTP tests for the minimal Phase 1 dashboard.

``db_session`` creates the schema on the same (temp) engine the app uses, so
combining it with ``client`` gives the endpoints real tables to work against.
"""

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from expense_analyzer.importers import NormalizedTransaction
from expense_analyzer.importers import registry as importer_registry
from expense_analyzer.models import Account, AccountType, Category, CategoryKind, Transaction


class FakeImporter:
    source = "Fake Bank csv"

    def parse(self, data: bytes) -> list[NormalizedTransaction]:
        return [
            NormalizedTransaction(date(2026, 5, 1), -12345, "Biedronka"),
            NormalizedTransaction(date(2026, 5, 2), 1000000, "Wyplata"),
        ]


@pytest.fixture
def fake_importer() -> Iterator[None]:
    importer_registry.register("fake", FakeImporter())
    try:
        yield
    finally:
        importer_registry._REGISTRY.pop("fake", None)


def test_index_renders(client: TestClient, db_session: Session):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "Expense Analyzer" in resp.text


def test_create_account_then_listed(client: TestClient, db_session: Session):
    resp = client.post(
        "/dashboard/accounts",
        data={"name": "PKO checking", "type": "bank"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert client.get("/dashboard").text.count("PKO checking") >= 1


def test_create_category(client: TestClient, db_session: Session):
    client.post("/dashboard/categories", data={"name": "Food", "kind": "expense"})
    cats = db_session.exec(select(Category)).all()
    assert [c.name for c in cats] == ["Food"]
    assert cats[0].kind == CategoryKind.expense


def test_upload_imports_transactions(client: TestClient, db_session: Session, fake_importer):
    acc = Account(name="PKO checking", type=AccountType.bank)
    db_session.add(acc)
    db_session.commit()
    db_session.refresh(acc)

    resp = client.post(
        "/dashboard/upload",
        data={"account_id": str(acc.id), "importer": "fake"},
        files={"file": ("may.csv", b"ignored-by-fake", "text/csv")},
    )
    assert resp.status_code == 200
    assert "Imported" in resp.text

    rows = db_session.exec(select(Transaction)).all()
    assert len(rows) == 2
    assert {r.amount for r in rows} == {-12345, 1000000}


def test_categorize_sets_category_and_scope(client: TestClient, db_session: Session, fake_importer):
    acc = Account(name="PKO", type=AccountType.bank)
    cat = Category(name="Food", kind=CategoryKind.expense)
    db_session.add_all([acc, cat])
    db_session.commit()
    db_session.refresh(acc)
    db_session.refresh(cat)

    client.post(
        "/dashboard/upload",
        data={"account_id": str(acc.id), "importer": "fake"},
        files={"file": ("may.csv", b"x", "text/csv")},
    )
    tx = db_session.exec(select(Transaction)).first()

    resp = client.post(
        f"/dashboard/transactions/{tx.id}/categorize",
        data={"category_id": str(cat.id), "scope": "household", "account_id": str(acc.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db_session.refresh(tx)
    assert tx.category_id == cat.id
    assert tx.scope.value == "household"
    assert tx.source.value == "manual"


def test_rollback_hides_transactions_from_list(
    client: TestClient, db_session: Session, fake_importer
):
    acc = Account(name="PKO", type=AccountType.bank)
    db_session.add(acc)
    db_session.commit()
    db_session.refresh(acc)

    up = client.post(
        "/dashboard/upload",
        data={"account_id": str(acc.id), "importer": "fake"},
        files={"file": ("may.csv", b"x", "text/csv")},
    )
    assert "Biedronka" in client.get("/dashboard/transactions").text

    batch_id = db_session.exec(select(Transaction)).first().import_batch_id
    client.post(f"/dashboard/batches/{batch_id}/rollback", follow_redirects=False)

    # Soft-deleted rows drop out of the transaction list.
    assert "Biedronka" not in client.get("/dashboard/transactions").text
    assert up.status_code == 200


def _account(db_session: Session) -> Account:
    acc = Account(name="PKO", type=AccountType.bank)
    db_session.add(acc)
    db_session.commit()
    db_session.refresh(acc)

    return acc


def test_upload_unknown_account_shows_error(client: TestClient, db_session: Session, fake_importer):
    resp = client.post(
        "/dashboard/upload",
        data={"account_id": "999", "importer": "fake"},
        files={"file": ("may.csv", b"x", "text/csv")},
    )
    assert resp.status_code == 200
    assert "Unknown account" in resp.text
    assert len(db_session.exec(select(Transaction)).all()) == 0  # nothing imported


def test_upload_unknown_importer_shows_error(client: TestClient, db_session: Session):
    acc = _account(db_session)
    resp = client.post(
        "/dashboard/upload",
        data={"account_id": str(acc.id), "importer": "nope"},
        files={"file": ("may.csv", b"x", "text/csv")},
    )
    assert resp.status_code == 200
    assert "Unknown importer" in resp.text


def test_upload_unparseable_file_shows_error(client: TestClient, db_session: Session):
    from expense_analyzer.importers.base import ImporterError

    class BoomImporter:
        source = "Boom csv"

        def parse(self, data: bytes):
            raise ImporterError("line 7: bad amount")

    importer_registry.register("boom", BoomImporter())
    try:
        acc = _account(db_session)
        resp = client.post(
            "/dashboard/upload",
            data={"account_id": str(acc.id), "importer": "boom"},
            files={"file": ("bad.csv", b"x", "text/csv")},
        )
        assert resp.status_code == 200
        assert "Could not parse the file" in resp.text
        assert "line 7: bad amount" in resp.text
        assert len(db_session.exec(select(Transaction)).all()) == 0
    finally:
        importer_registry._REGISTRY.pop("boom", None)


def test_categorize_rejects_non_numeric_category(client: TestClient, db_session: Session):
    resp = client.post(
        "/dashboard/transactions/1/categorize",
        data={"category_id": "abc", "scope": "private"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_categorize_rejects_unknown_category(
    client: TestClient, db_session: Session, fake_importer
):
    acc = _account(db_session)
    client.post(
        "/dashboard/upload",
        data={"account_id": str(acc.id), "importer": "fake"},
        files={"file": ("may.csv", b"x", "text/csv")},
    )
    tx = db_session.exec(select(Transaction)).first()
    resp = client.post(
        f"/dashboard/transactions/{tx.id}/categorize",
        data={"category_id": "9999", "scope": "private"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_categorize_unknown_transaction_404(client: TestClient, db_session: Session):
    resp = client.post(
        "/dashboard/transactions/12345/categorize",
        data={"category_id": "", "scope": "private"},
        follow_redirects=False,
    )
    assert resp.status_code == 404
