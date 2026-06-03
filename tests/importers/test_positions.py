"""import_positions upsert semantics (latest-wins per snapshot date)."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlmodel import Session, select

from expense_analyzer.importers.positions import (
    NormalizedPosition,
    PositionsResult,
    import_positions,
)
from expense_analyzer.models import Account, AccountType, InvestmentPosition

_DAY = date(2026, 4, 15)


def _pos(ticker: str, value: int, snapshot: date = _DAY) -> NormalizedPosition:
    return NormalizedPosition(
        ticker=ticker, quantity=Decimal("1"), value=value, snapshot_date=snapshot
    )


def _import(session: Session, account_id: int, positions: list[NormalizedPosition]):
    return import_positions(
        session,
        account_id=account_id,
        result=PositionsResult(positions=positions),
        source="xtb",
        fetched_at=datetime.now(UTC),
    )


def test_reimport_same_snapshot_updates_not_duplicates(
    db_session: Session,
    make_account: Callable[..., Account],
) -> None:
    acc = make_account(name="IKE", type=AccountType.portfolio)

    first = _import(db_session, acc.id, [_pos("SNT.PL", 900_00), _pos("SXR8.DE", 1265_12)])
    assert (first.inserted, first.updated) == (2, 0)

    # Same date, changed values → all updates, zero new rows.
    second = _import(db_session, acc.id, [_pos("SNT.PL", 950_00), _pos("SXR8.DE", 1300_00)])
    assert (second.inserted, second.updated) == (0, 2)

    rows = db_session.exec(
        select(InvestmentPosition).where(InvestmentPosition.account_id == acc.id)
    ).all()
    assert len(rows) == 2  # no duplicates
    assert {r.value for r in rows} == {950_00, 1300_00}  # latest wins


def test_different_snapshot_dates_coexist(
    db_session: Session,
    make_account: Callable[..., Account],
) -> None:
    acc = make_account(name="IKE", type=AccountType.portfolio)

    _import(db_session, acc.id, [_pos("SNT.PL", 900_00, date(2026, 3, 15))])
    _import(db_session, acc.id, [_pos("SNT.PL", 950_00, date(2026, 4, 15))])

    rows = db_session.exec(
        select(InvestmentPosition).where(InvestmentPosition.account_id == acc.id)
    ).all()
    assert len(rows) == 2  # one row per snapshot date


def test_repeated_ticker_in_one_batch_does_not_duplicate(
    db_session: Session,
    make_account: Callable[..., Account],
) -> None:
    acc = make_account(name="IKE", type=AccountType.portfolio)

    # Same (ticker, date) twice in one batch — the second updates the first,
    # so the unique key is never violated.
    summary = _import(db_session, acc.id, [_pos("SNT.PL", 900_00), _pos("SNT.PL", 950_00)])

    rows = db_session.exec(
        select(InvestmentPosition).where(InvestmentPosition.account_id == acc.id)
    ).all()
    assert len(rows) == 1
    assert rows[0].value == 950_00
    assert summary.inserted == 1 and summary.updated == 1
