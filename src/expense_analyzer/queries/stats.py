"""Spending / income statistics for the dashboard overview (Phase 4).

**Transfers are excluded from every figure here.** Moving money between your own
accounts is neither spending nor income (design §6), so leaving it in would show
a fake expense on one account and a fake inflow on the other. A row counts as a
transfer if it carries a ``transfer_group_id`` *or* sits in a ``kind=transfer``
category — the two signals Phase 3 writes on a confirmed transfer (the second
also catches a leg a human manually tagged ``Transfer`` without auto-linking).

Money stays integer minor units throughout (never float; design §5). Spending
and income are reported as positive magnitudes; ``net = income - spending``.

Bucketing is by the bank's local ``booked_date`` — already a local calendar date
(see clock.py), so a month key is just its ``YYYY-MM`` prefix.

DB access is isolated to :func:`spendable_transactions` and
:func:`available_months`; the summary/trend functions are pure over a preloaded
row list, so the overview page loads the (single) spendable scan once and feeds
both. For one household (a few thousand rows a year) that scan is cheap and stays
free of SQLite date-function quirks, matching how the transfer detector already
walks the table.
"""

from collections import defaultdict
from dataclasses import dataclass

from sqlmodel import Session, col, select

from expense_analyzer.models import Category, CategoryKind, Transaction

UNCATEGORIZED_LABEL = "Uncategorized"


@dataclass(frozen=True)
class CategoryTotal:
    category_id: int | None  # None == uncategorized
    name: str
    total: int  # positive magnitude of spending, minor units


@dataclass(frozen=True)
class MonthTotals:
    month: str  # "2026-05"
    spending: int  # positive magnitude
    income: int  # positive magnitude

    @property
    def net(self) -> int:
        return self.income - self.spending


@dataclass(frozen=True)
class MonthSummary:
    month: str
    spending: int
    income: int
    by_category: list[CategoryTotal]  # expenses by category, largest first

    @property
    def net(self) -> int:
        return self.income - self.spending


def spendable_transactions(session: Session) -> list[Transaction]:
    """Live transactions that count toward spending/income (transfers excluded).

    ``transfer_group_id IS NULL`` drops auto/confirmed transfers in SQL; the
    rare manually-``Transfer``-tagged leg with no group is filtered out in Python
    against the transfer category ids. Load once and pass the result to
    :func:`month_summary` / :func:`spending_trend`.
    """
    transfer_category_ids = {
        c.id for c in session.exec(select(Category).where(Category.kind == CategoryKind.transfer))
    }
    rows = session.exec(
        select(Transaction).where(
            col(Transaction.deleted_at).is_(None),
            col(Transaction.transfer_group_id).is_(None),
        )
    ).all()

    return [tx for tx in rows if tx.category_id not in transfer_category_ids]


def available_months(session: Session) -> list[str]:
    """Distinct ``YYYY-MM`` months with any live (non-deleted) transaction,
    newest first — used to populate month pickers (transfers included here, so
    the transaction list can still be filtered to a transfer-only month)."""
    rows = session.exec(
        select(col(Transaction.booked_date)).where(col(Transaction.deleted_at).is_(None))
    ).all()

    return sorted({d.strftime("%Y-%m") for d in rows}, reverse=True)


def month_summary(
    transactions: list[Transaction], month: str, category_names: dict[int, str]
) -> MonthSummary:
    """Spending, income and per-category expense breakdown for one ``YYYY-MM``.

    ``transactions`` must already be transfer-excluded (see
    :func:`spendable_transactions`)."""
    spending = 0
    income = 0
    by_category: dict[int | None, int] = defaultdict(int)

    for tx in transactions:
        if tx.booked_date.strftime("%Y-%m") != month:
            continue
        if tx.amount < 0:
            spending -= tx.amount  # magnitude
            by_category[tx.category_id] += -tx.amount
        elif tx.amount > 0:
            income += tx.amount

    categories = [
        CategoryTotal(
            category_id=cid,
            name=UNCATEGORIZED_LABEL if cid is None else category_names.get(cid, f"#{cid}"),
            total=total,
        )
        for cid, total in by_category.items()
    ]
    categories.sort(key=lambda c: c.total, reverse=True)

    return MonthSummary(month=month, spending=spending, income=income, by_category=categories)


def spending_trend(transactions: list[Transaction], *, months: int) -> list[MonthTotals]:
    """Spending and income per month for the last ``months`` months that have
    spendable activity, oldest first (left-to-right on a trend chart).

    ``transactions`` must already be transfer-excluded (see
    :func:`spendable_transactions`)."""
    spending: dict[str, int] = defaultdict(int)
    income: dict[str, int] = defaultdict(int)

    for tx in transactions:
        key = tx.booked_date.strftime("%Y-%m")
        if tx.amount < 0:
            spending[key] -= tx.amount
        elif tx.amount > 0:
            income[key] += tx.amount

    active = sorted(set(spending) | set(income))[-months:]

    return [MonthTotals(month=m, spending=spending[m], income=income[m]) for m in active]
