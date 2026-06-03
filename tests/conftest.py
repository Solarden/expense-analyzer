"""Shared test fixtures.

The database path is redirected to a throwaway temp file so tests never touch
the real ``data/`` database. This is safe to do after imports because the
engine is built lazily (see expense_analyzer.db.get_engine) — nothing opens a
connection at import time.
"""

import itertools
import os
import tempfile
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel

from expense_analyzer.config import get_settings
from expense_analyzer.importers import NormalizedTransaction, ParseResult
from expense_analyzer.importers import registry as importer_registry
from expense_analyzer.models import (
    Account,
    AccountType,
    Category,
    CategoryKind,
    ImportBatch,
    Transaction,
)

os.environ["EA_DATABASE_PATH"] = str(Path(tempfile.mkdtemp(prefix="ea-test-")) / "test.db")
os.environ.setdefault("EA_SECRET_KEY", "test-secret-not-for-production")  # app refuses default
get_settings.cache_clear()  # drop any settings cached before the override


@pytest.fixture(autouse=True, scope="session")
def _fast_bcrypt() -> Iterator[None]:
    """Use minimal bcrypt rounds in tests — real cost (12) makes the suite slow."""
    import bcrypt

    original = bcrypt.gensalt
    bcrypt.gensalt = lambda rounds=4, prefix=b"2b": original(rounds=4, prefix=prefix)
    try:
        yield
    finally:
        bcrypt.gensalt = original


# Committed test fixtures (anonymized sample data). Resolved from this conftest
# so tests find them regardless of which subdirectory they live in.
FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def client() -> Iterator[TestClient]:
    from expense_analyzer.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client(client: TestClient, db_session: Session) -> TestClient:
    """A TestClient already logged in as a freshly-created user.

    The session cookie set by /login persists on the client, so subsequent
    requests hit the (auth-protected) dashboard as that user.
    """
    from expense_analyzer.queries import users

    users.create_user(db_session, username="tester", name="Tester", password="secret123")
    resp = client.post(
        "/login",
        data={"username": "tester", "password": "secret123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    return client


@pytest.fixture
def db_session() -> Iterator[Session]:
    """A session against a fresh schema. Tables are created before and dropped
    after each test, so model tests start from a clean slate.

    In Phase 0 there are no models yet, so this just exercises the engine."""
    from expense_analyzer.db import get_engine

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        SQLModel.metadata.drop_all(engine)


# --- Model & importer builders --------------------------------------------
# Factory-as-fixture: each `make_*` fixture returns a callable, so a test that
# needs several varied instances just calls it again. This covers the case that
# would otherwise tempt factory_boy — without a new dependency or session wiring
# (factory_boy is deferred until the model count/complexity actually grows, e.g.
# Loan/Budget/InvestmentPosition in Phase 5-6; see internal_docs/PROGRESS.md).


class FakeImporter:
    """An Importer that returns canned records (plus optional declared totals).

    Shared by the dashboard upload tests (via the ``fake_importer`` registry
    fixture) and the pipeline tests (built directly through ``make_importer``).
    """

    source = "Fake Bank csv"

    def __init__(
        self,
        records: list[NormalizedTransaction],
        *,
        declared_inflow: int | None = None,
        declared_outflow: int | None = None,
    ) -> None:
        self._records = records
        self._declared_inflow = declared_inflow
        self._declared_outflow = declared_outflow

    def parse(self, data: bytes) -> ParseResult:
        return ParseResult(
            transactions=self._records,
            declared_inflow=self._declared_inflow,
            declared_outflow=self._declared_outflow,
        )


# Unique-across-a-run counter so auto-generated fingerprints never collide on
# the unique index, even across accounts/tests.
_fingerprint_seq = itertools.count(1)


@pytest.fixture
def make_account(db_session: Session) -> Callable[..., Account]:
    def _make(name: str = "PKO checking", type: AccountType = AccountType.bank, **kw) -> Account:
        acc = Account(name=name, type=type, **kw)
        db_session.add(acc)
        db_session.commit()
        db_session.refresh(acc)
        return acc

    return _make


@pytest.fixture
def account(make_account: Callable[..., Account]) -> Account:
    """A ready-made bank account — the common 'I just need an account' case."""
    return make_account()


@pytest.fixture
def make_category(db_session: Session) -> Callable[..., Category]:
    def _make(name: str = "Food", kind: CategoryKind = CategoryKind.expense, **kw) -> Category:
        cat = Category(name=name, kind=kind, **kw)
        db_session.add(cat)
        db_session.commit()
        db_session.refresh(cat)
        return cat

    return _make


@pytest.fixture
def make_batch(db_session: Session) -> Callable[..., ImportBatch]:
    def _make(source: str = "test", filename: str = "test.csv", **kw) -> ImportBatch:
        batch = ImportBatch(source=source, filename=filename, **kw)
        db_session.add(batch)
        db_session.commit()
        db_session.refresh(batch)
        return batch

    return _make


@pytest.fixture
def make_transaction(
    db_session: Session, make_batch: Callable[..., ImportBatch]
) -> Callable[..., Transaction]:
    def _make(
        *,
        account_id: int,
        amount: int,
        day: int = 1,
        booked_date: date | None = None,
        import_batch_id: int | None = None,
        raw_description: str | None = None,
        fingerprint: str | None = None,
        **kw,
    ) -> Transaction:
        # One throwaway batch per transaction unless the caller pins import_batch_id
        # — keeps single-tx tests trivial; pass it explicitly to group rows in a batch.
        if import_batch_id is None:
            import_batch_id = make_batch().id
        n = next(_fingerprint_seq)
        tx = Transaction(
            account_id=account_id,
            import_batch_id=import_batch_id,
            amount=amount,
            booked_date=booked_date or date(2026, 5, day),
            raw_description=raw_description or f"row {n}",
            fingerprint=fingerprint or f"fp-{n}",
            **kw,
        )
        db_session.add(tx)
        db_session.commit()
        db_session.refresh(tx)
        return tx

    return _make


@pytest.fixture
def make_importer() -> Callable[..., FakeImporter]:
    def _make(
        records: list[NormalizedTransaction],
        *,
        declared_inflow: int | None = None,
        declared_outflow: int | None = None,
    ) -> FakeImporter:
        return FakeImporter(
            records, declared_inflow=declared_inflow, declared_outflow=declared_outflow
        )

    return _make


@pytest.fixture
def fake_importer() -> Iterator[str]:
    """Register a default fake importer under the slug ``fake``; yields the slug.

    Two records with distinct amounts (no equal-and-opposite pair), so importing
    them never trips transfer auto-linking.
    """
    importer_registry.register(
        "fake",
        FakeImporter(
            [
                NormalizedTransaction(date(2026, 5, 1), -12345, "Biedronka"),
                NormalizedTransaction(date(2026, 5, 2), 1000000, "Wyplata"),
            ]
        ),
    )
    try:
        yield "fake"
    finally:
        importer_registry.unregister("fake")
