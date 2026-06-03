"""Investment positions — the shared, source-agnostic snapshot path (design §7.3).

Investment positions are *not* transactions, so they don't go through the
fingerprint/batch import pipeline. A monthly portfolio export (or an API pull) is
a **state snapshot**: a set of holdings as of one date. We upsert on the natural
key ``(account_id, ticker, snapshot_date)`` — re-importing the same day's data
updates the rows instead of duplicating them.

Two sources feed this, both producing a :class:`PositionsResult`:

- :class:`~expense_analyzer.importers.xtb.XTBImporter` parses an uploaded XTB
  ``.xlsx`` (offline).
- :class:`~expense_analyzer.importers.myfund.MyFundClient` pulls from the
  myFund.pl API (opt-in network egress).

Money is integer minor units throughout; ``quantity`` is a fractional unit count
(:class:`~decimal.Decimal`), the one non-money number here.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlmodel import Session, col, select

from expense_analyzer.models import InvestmentPosition
from expense_analyzer.money import format_pln


@dataclass(frozen=True, slots=True)
class NormalizedPosition:
    """One holding in the common, source-agnostic format. Money is minor units."""

    ticker: str
    quantity: Decimal  # fractional unit count (not money)
    value: int  # minor units: current market value of the holding
    snapshot_date: date
    avg_price: int | None = None  # minor units, average purchase price per unit
    current_price: int | None = None  # minor units, last price per unit
    currency: str = "PLN"


@dataclass(frozen=True, slots=True)
class PositionsResult:
    """Everything a source extracted from one snapshot.

    Beyond the holdings, a source may state an account total it can be checked
    against (XTB prints ``Equity``; myFund returns the portfolio ``wartosc``) and
    a cash balance not represented as a position (XTB ``Balance``). Both optional.
    """

    positions: list[NormalizedPosition]
    declared_total: int | None = None  # minor units, source-stated account value
    cash_balance: int | None = None  # minor units, cash not held as a position


@dataclass(frozen=True, slots=True)
class PositionsReconciliation:
    """Non-blocking sanity check: source-declared total vs what we imported."""

    ok: bool
    label: str  # "OK" / "Mismatch" / "Not available"
    details: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PositionsSummary:
    account_id: int
    snapshot_date: date | None
    inserted: int
    updated: int
    imported_total: int  # minor units, sum of imported position values
    reconciliation: PositionsReconciliation

    @property
    def imported(self) -> int:
        return self.inserted + self.updated


def reconcile_positions(result: PositionsResult, imported_total: int) -> PositionsReconciliation:
    """Compare the source-declared account total against cash + imported value.

    Non-blocking, mirrors the transaction reconciler: a mismatch is a warning, not
    an error — the snapshot still imports, but the user is told to look.
    """
    if result.declared_total is None:
        return PositionsReconciliation(
            ok=True,
            label="Not available",
            details=["This snapshot states no account total to check against."],
        )

    cash = result.cash_balance or 0
    computed = cash + imported_total
    if computed == result.declared_total:
        return PositionsReconciliation(
            ok=True,
            label="OK",
            details=[
                f"Positions {format_pln(imported_total)} + cash {format_pln(cash)} "
                f"matches declared {format_pln(result.declared_total)}."
            ],
        )

    return PositionsReconciliation(
        ok=False,
        label="Mismatch",
        details=[
            f"Positions {format_pln(imported_total)} + cash {format_pln(cash)} = "
            f"{format_pln(computed)} ≠ declared {format_pln(result.declared_total)}."
        ],
    )


def import_positions(
    session: Session,
    *,
    account_id: int,
    result: PositionsResult,
    source: str,
    fetched_at: datetime,
) -> PositionsSummary:
    """Upsert a snapshot of holdings into ``account_id`` (latest-wins per date).

    Each position is keyed by ``(account_id, ticker, snapshot_date)``: a new key
    inserts, an existing one updates in place. Commits once.

    Existing rows for the snapshot dates in this batch are loaded in **one** query
    up front (not one SELECT per position). The lookup dict is updated as we insert,
    so a ticker repeated within the same batch updates the row just created rather
    than inserting a duplicate (which would trip the unique key).
    """
    snapshot_dates = list({p.snapshot_date for p in result.positions})
    rows = (
        session.exec(
            select(InvestmentPosition).where(
                InvestmentPosition.account_id == account_id,
                col(InvestmentPosition.snapshot_date).in_(snapshot_dates),
            )
        ).all()
        if snapshot_dates
        else []
    )
    by_key: dict[tuple[str, date], InvestmentPosition] = {
        (r.ticker, r.snapshot_date): r for r in rows
    }

    inserted = 0
    updated = 0
    for p in result.positions:
        existing = by_key.get((p.ticker, p.snapshot_date))
        if existing is None:
            row = InvestmentPosition(
                account_id=account_id,
                ticker=p.ticker,
                quantity=p.quantity,
                value=p.value,
                avg_price=p.avg_price,
                current_price=p.current_price,
                currency=p.currency,
                snapshot_date=p.snapshot_date,
                source=source,
                fetched_at=fetched_at,
            )
            session.add(row)
            by_key[(p.ticker, p.snapshot_date)] = row  # so a repeated ticker updates it
            inserted += 1
        else:
            existing.quantity = p.quantity
            existing.value = p.value
            existing.avg_price = p.avg_price
            existing.current_price = p.current_price
            existing.currency = p.currency
            existing.source = source
            existing.fetched_at = fetched_at
            session.add(existing)
            updated += 1

    session.commit()

    imported_total = sum(p.value for p in result.positions)
    snapshot_date = result.positions[0].snapshot_date if result.positions else None

    return PositionsSummary(
        account_id=account_id,
        snapshot_date=snapshot_date,
        inserted=inserted,
        updated=updated,
        imported_total=imported_total,
        reconciliation=reconcile_positions(result, imported_total),
    )
