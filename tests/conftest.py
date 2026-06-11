"""Shared test fixtures.

The suite runs against a real PostgreSQL by default (prod parity — production
is the shared /opt/stack server): the throwaway container from
``docker-compose.test.yml``, started by ``make test-db-up``. Set
``EA_TEST_DATABASE_URL`` (e.g. a sqlite URL) for a quick docker-less run.

Redirecting the URL here is safe because the engine is built lazily (see
expense_analyzer.db.get_engine) — nothing opens a connection at import time.
"""

import itertools
import os
import tempfile
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel

from expense_analyzer.clock import utc_now
from expense_analyzer.config import get_settings
from expense_analyzer.importers import NormalizedTransaction, ParseResult
from expense_analyzer.importers import registry as importer_registry
from expense_analyzer.models import (
    Account,
    AccountType,
    Budget,
    Category,
    CategoryKind,
    ImportBatch,
    InstallmentType,
    InvestmentPosition,
    Loan,
    PlannedItem,
    RateType,
    Rule,
    Subscription,
    SubscriptionStatus,
    Transaction,
)

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="ea-test-"))
os.environ["EA_DATABASE_URL"] = os.environ.get(
    "EA_TEST_DATABASE_URL",
    "postgresql+psycopg://ea_test:ea_test@localhost:55432/ea_test",
)
# Loan attachments (Phase 21) land in a throwaway temp dir, never the real data/.
os.environ["EA_ATTACHMENTS_PATH"] = str(_TEST_DATA_DIR / "attachments")
os.environ.setdefault("EA_SECRET_KEY", "test-secret-not-for-production")  # app refuses default
# Layer 3 (Phase 12) off by default in the suite: its logic is tested directly with
# an injected fake embedder, so the real path must never load the heavy
# sentence-transformers model (slow, and would reach the network for the weights).
# Endpoint tests then exercise the fail-safe render (no suggestions, page still OK).
os.environ.setdefault("EA_EMBEDDINGS_ENABLED", "false")
get_settings.cache_clear()  # drop any settings cached before the override


@pytest.fixture(autouse=True, scope="session")
def _database() -> Iterator[Engine]:
    """Connectivity gate + one schema for the whole run.

    Creating and dropping every table per test is cheap on SQLite but expensive
    on PostgreSQL (17 tables + native enum types, hundreds of times over), so
    the schema is built once per session; per-test isolation is the wipe in
    ``db_session``'s teardown instead.
    """
    from expense_analyzer.db import get_engine

    engine = get_engine()
    try:
        with engine.connect():
            pass
    except OperationalError as exc:
        pytest.exit(
            f"test database unreachable ({engine.url.render_as_string()}) — "
            f"run `make test-db-up`, or set EA_TEST_DATABASE_URL: {exc}",
            returncode=4,
        )

    SQLModel.metadata.drop_all(engine)  # leftovers from an aborted earlier run
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        SQLModel.metadata.drop_all(engine)


def _reset_all_tables(engine: Engine) -> None:
    """Wipe every table (and restart id sequences) between tests.

    On PostgreSQL a single TRUNCATE handles FK ordering and identity reset in
    one statement. On SQLite (the EA_TEST_DATABASE_URL quick path) there is no
    TRUNCATE; deleting in reverse dependency order respects FKs, and rowid
    numbering restarts by itself once a table is empty.
    """
    tables = SQLModel.metadata.sorted_tables
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            names = ", ".join(f'"{t.name}"' for t in tables)
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        else:
            # Deferred FK checks: reverse dependency order handles FKs *between*
            # tables, but not the self-referential category.parent_id — deleting
            # parents before children inside one table would trip foreign_keys=ON.
            # Deferring validates at commit, when everything is already gone.
            conn.execute(text("PRAGMA defer_foreign_keys=ON"))
            for table in reversed(tables):
                conn.execute(table.delete())


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
    from expense_analyzer.queries.core import users

    users.create_user(db_session, username="tester", name="Tester", password="secret123")
    resp = client.post(
        "/login",
        data={"username": "tester", "password": "secret123"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER

    return client


@pytest.fixture
def db_session(_database: Engine) -> Iterator[Session]:
    """A session against the shared session-scoped schema.

    Every table is wiped after each test (see ``_reset_all_tables``), so tests
    start from a clean slate — same semantics as the old per-test
    create_all/drop_all, at a fraction of the PostgreSQL cost."""
    try:
        with Session(_database) as session:
            yield session
    finally:
        _reset_all_tables(_database)


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
def make_budget(db_session: Session) -> Callable[..., Budget]:
    def _make(
        *, category_id: int, month: str | None = None, limit_amount: int = 200_000, **kw
    ) -> Budget:
        budget = Budget(category_id=category_id, month=month, limit_amount=limit_amount, **kw)
        db_session.add(budget)
        db_session.commit()
        db_session.refresh(budget)
        return budget

    return _make


@pytest.fixture
def make_subscription(db_session: Session) -> Callable[..., Subscription]:
    def _make(
        *, merchant: str, status: SubscriptionStatus = SubscriptionStatus.confirmed, **kw
    ) -> Subscription:
        subscription = Subscription(merchant=merchant, status=status, **kw)
        db_session.add(subscription)
        db_session.commit()
        db_session.refresh(subscription)
        return subscription

    return _make


@pytest.fixture
def make_rule(db_session: Session) -> Callable[..., Rule]:
    def _make(*, pattern: str, category_id: int, priority: int = 0, **kw) -> Rule:
        rule = Rule(pattern=pattern, category_id=category_id, priority=priority, **kw)
        db_session.add(rule)
        db_session.commit()
        db_session.refresh(rule)
        return rule

    return _make


@pytest.fixture
def make_planned_item(db_session: Session) -> Callable[..., PlannedItem]:
    def _make(*, name: str = "Rent", expected_amount: int | None = -200_000, **kw) -> PlannedItem:
        item = PlannedItem(name=name, expected_amount=expected_amount, **kw)
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        return item

    return _make


@pytest.fixture
def make_loan(db_session: Session) -> Callable[..., Loan]:
    def _make(
        *,
        account_id: int,
        principal: int = 30_000_000,
        rate_type: RateType = RateType.fixed,
        rate_bp: int = 720,
        installment_type: InstallmentType = InstallmentType.equal,
        start_date: date | None = None,
        term_months: int = 12,
        **kw,
    ) -> Loan:
        loan = Loan(
            account_id=account_id,
            principal=principal,
            rate_type=rate_type,
            rate_bp=rate_bp,
            installment_type=installment_type,
            start_date=start_date or date(2026, 1, 15),
            term_months=term_months,
            **kw,
        )
        db_session.add(loan)
        db_session.commit()
        db_session.refresh(loan)
        return loan

    return _make


_XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _build_xtb_xlsx(
    *,
    sheet_name: str = "OPEN POSITION 15042026",
    balance: str = "16.57",
    equity: str = "2181.69",
    lots: list[dict] | None = None,
) -> bytes:
    """Build an XTB-shaped .xlsx in memory (stdlib zip + XML).

    Faithful to a real export: a blank leading column, the full ``Position..Comment``
    table starting at column B, the ``Name/Account`` + ``Balance/Equity`` header
    block, inline strings (no sharedStrings), and four sheets so the parser must
    pick ``OPEN POSITION`` among siblings. Numbers are raw strings (as real exports
    store them) so the parser exercises its Decimal path. ``lots`` items:
    ``{symbol, volume, market, purchase, pl}``; the default set sums to the default
    ``equity`` (cash + Σ value) so reconciliation passes. No real account/holdings.

    Used by the XTB parser tests, the upload API test, and to (re)generate the
    committed regression fixtures in ``tests/fixtures/xtb/`` (see its README).
    """
    import io
    import zipfile

    if lots is None:
        lots = [
            {
                "symbol": "SXR8.DE",
                "volume": "1",
                "market": "632.56",
                "purchase": "565.78",
                "pl": "66.78",
            },
            {
                "symbol": "SXR8.DE",
                "volume": "1",
                "market": "632.56",
                "purchase": "595.72",
                "pl": "36.84",
            },
            {
                "symbol": "SNT.PL",
                "volume": "3",
                "market": "300.00",
                "purchase": "777.00",
                "pl": "123.00",
            },
        ]

    # Real XTB layout: a blank leading column A, the positions table runs B..Q,
    # and a header block sits above it (cols by 0-based index; F=5, I=8, P=15, Q=16).
    cols = "ABCDEFGHIJKLMNOPQR"
    T, N = True, False
    rows: list[str] = []

    def emit(r: int, cells: dict[int, tuple[str, bool]]) -> None:
        inner = "".join(
            (
                f'<c r="{cols[i]}{r}" t="inlineStr"><is><t>{v}</t></is></c>'
                if text
                else f'<c r="{cols[i]}{r}"><v>{v}</v></c>'
            )
            for i, (v, text) in sorted(cells.items())
            if v != ""
        )
        rows.append(f'<row r="{r}">{inner}</row>')

    # Header block — labels one row above their values, exactly as a real export.
    emit(
        3, {5: ("Name and surname", T), 8: ("Account", T), 11: ("Currency", T), 16: ("45762.5", N)}
    )
    emit(4, {8: ("00000000", T)})  # account number — anonymized
    emit(
        5,
        {
            5: ("Balance", T),
            8: ("Equity", T),
            11: ("Margin", T),
            13: ("Free margin", T),
            16: ("Margin level", T),
        },
    )
    emit(6, {5: (balance, N), 8: (equity, N), 11: ("0.00", N), 13: (balance, N), 16: ("0.00", N)})
    emit(8, {1: ("45762.5", N)})  # export timestamp serial (ignored by the parser)
    # Positions table header — located by the "Position"/"Symbol" cells.
    emit(
        9,
        {
            1: ("Position", T),
            2: ("Symbol", T),
            3: ("Type", T),
            4: ("Volume", T),
            5: ("Open time", T),
            6: ("Open price", T),
            7: ("Market price", T),
            8: ("Purchase value", T),
            9: ("SL", T),
            10: ("TP", T),
            11: ("Margin", T),
            12: ("Commission", T),
            13: ("Swap", T),
            14: ("Rollover", T),
            15: ("Gross P/L", T),
            16: ("Comment", T),
        },
    )
    for i, lot in enumerate(lots):
        emit(
            10 + i,
            {
                1: (str(1900000000 + i), N),
                2: (lot["symbol"], T),
                3: ("BUY", T),
                4: (lot["volume"], N),
                5: ("45800.0", N),
                6: (lot.get("open", "100.00"), N),
                7: (lot["market"], N),
                8: (lot["purchase"], N),
                11: ("0.00", N),
                12: ("0.00", N),
                13: ("0.00", N),
                14: ("0.00", N),
                15: (lot["pl"], N),
            },
        )

    open_sheet = (
        f'<?xml version="1.0"?><worksheet xmlns="{_XLSX_NS}"><sheetData>'
        + "".join(rows)
        + "</sheetData></worksheet>"
    )
    empty_sheet = f'<?xml version="1.0"?><worksheet xmlns="{_XLSX_NS}"><sheetData/></worksheet>'

    # Four sheets like a real export; OPEN POSITION carries the data, the others
    # are present-but-empty so the parser must select the right one among siblings.
    sheet_defs = [
        ("CLOSED POSITION HISTORY", "sheet1.xml", empty_sheet),
        (sheet_name, "sheet2.xml", open_sheet),
        ("PENDING ORDERS HISTORY", "sheet3.xml", empty_sheet),
        ("CASH OPERATION HISTORY", "sheet4.xml", empty_sheet),
    ]
    sheets_xml = "".join(
        f'<sheet name="{name}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
        for i, (name, _f, _x) in enumerate(sheet_defs)
    )
    rels_xml = "".join(
        f'<Relationship Id="rId{i + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/{f}"/>'
        for i, (_name, f, _x) in enumerate(sheet_defs)
    )
    workbook = (
        f'<?xml version="1.0"?><workbook xmlns="{_XLSX_NS}" xmlns:r="{_XLSX_NS_REL}">'
        f"<sheets>{sheets_xml}</sheets></workbook>"
    )
    rels = (
        '<?xml version="1.0"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f"{rels_xml}</Relationships>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        for _name, f, x in sheet_defs:
            zf.writestr(f"xl/worksheets/{f}", x)

    return buf.getvalue()


@pytest.fixture
def xtb_xlsx() -> Callable[..., bytes]:
    """Returns the in-memory XTB .xlsx builder (see :func:`_build_xtb_xlsx`)."""
    return _build_xtb_xlsx


@pytest.fixture
def make_investment(db_session: Session) -> Callable[..., InvestmentPosition]:
    def _make(
        *,
        account_id: int,
        ticker: str = "SXR8.DE",
        quantity: Decimal | str = "1",
        value: int = 100_00,
        snapshot_date: date | None = None,
        source: str = "xtb",
        **kw,
    ) -> InvestmentPosition:
        pos = InvestmentPosition(
            account_id=account_id,
            ticker=ticker,
            quantity=Decimal(str(quantity)),
            value=value,
            snapshot_date=snapshot_date or date(2026, 4, 15),
            source=source,
            fetched_at=utc_now(),
            **kw,
        )
        db_session.add(pos)
        db_session.commit()
        db_session.refresh(pos)
        return pos

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
