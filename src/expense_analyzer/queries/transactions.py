"""Transaction queries — the read/write side of the transaction list.

The list is filtered and paginated (Phase 4): the old unpaginated 500-row cap is
gone, replaced by ``page_size`` windows so an old, large DB renders one cheap
page at a time. Filters and pagination share one ``_apply_filters`` builder so
the page query and its ``COUNT`` can never drift apart.
"""

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from expense_analyzer.models import Scope, Transaction, TxSource

# Sentinel for the category filter: match rows with no category at all.
UNCATEGORIZED = "none"

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


@dataclass(frozen=True)
class TransactionFilters:
    account_id: int | None = None
    month: str | None = None  # "YYYY-MM"
    category_id: int | None = None  # set with uncategorized=False
    uncategorized: bool = False  # category_id IS NULL
    scope: Scope | None = None
    search: str | None = None  # substring in raw_description / merchant_normalized


@dataclass(frozen=True)
class TransactionPage:
    rows: list[Transaction]
    total: int  # matching rows across all pages
    page: int  # 1-based
    page_size: int

    @property
    def pages(self) -> int:
        size = max(1, self.page_size)  # guard against a 0 page size (no div-by-zero)
        return max(1, -(-self.total // size))  # ceil div, never 0

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def _month_bounds(month: str) -> tuple[date, date] | None:
    """``"2026-05"`` -> half-open ``[2026-05-01, 2026-06-01)`` for an index-friendly
    range filter on the (indexed) ``booked_date`` column.

    Returns ``None`` for anything that isn't a valid ``YYYY-MM`` (e.g. a
    hand-crafted ``?month=abc`` or ``?month=2026-13``) so the caller can skip the
    filter instead of blowing up with a 500."""
    if not _MONTH_RE.match(month):
        return None
    year, mon = int(month[:4]), int(month[5:7])
    if not 1 <= mon <= 12:
        return None
    start = date(year, mon, 1)
    end = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)

    return start, end


def _apply_filters(
    query: SelectOfScalar[Transaction], filters: TransactionFilters
) -> SelectOfScalar[Transaction]:
    query = query.where(col(Transaction.deleted_at).is_(None))
    if filters.account_id is not None:
        query = query.where(Transaction.account_id == filters.account_id)
    if filters.month is not None:
        bounds = _month_bounds(filters.month)
        if bounds is not None:  # invalid month string -> no date filter, not a crash
            start, end = bounds
            query = query.where(Transaction.booked_date >= start, Transaction.booked_date < end)
    if filters.uncategorized:
        query = query.where(col(Transaction.category_id).is_(None))
    elif filters.category_id is not None:
        query = query.where(Transaction.category_id == filters.category_id)
    if filters.scope is not None:
        query = query.where(Transaction.scope == filters.scope)
    if filters.search and filters.search.strip():
        # Escape LIKE wildcards so a literal % or _ in the search box doesn't
        # silently widen the match. NOTE: SQLite's ilike lowercases ASCII only,
        # so Polish diacritics (Ł, Ą…) match case-sensitively — acceptable for a
        # household search box.
        term = filters.search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{term}%"
        query = query.where(
            col(Transaction.raw_description).ilike(like, escape="\\")
            | col(Transaction.merchant_normalized).ilike(like, escape="\\")
        )

    return query


def list_transactions(
    session: Session,
    filters: TransactionFilters,
    *,
    page: int,
    page_size: int,
) -> TransactionPage:
    """One page of non-deleted transactions (newest first) matching ``filters``."""
    page = max(1, page)

    total = session.exec(
        _apply_filters(select(func.count()).select_from(Transaction), filters)  # type: ignore[arg-type]
    ).one()

    rows_query = _apply_filters(select(Transaction), filters)
    rows_query = (
        rows_query.order_by(col(Transaction.booked_date).desc(), col(Transaction.id).desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    return TransactionPage(
        rows=list(session.exec(rows_query).all()),
        total=int(total),
        page=page,
        page_size=page_size,
    )


def set_category(
    session: Session,
    *,
    tx_id: int,
    category_id: int | None,
    scope: Scope,
) -> Transaction | None:
    """Manually (re)categorize a transaction. Returns the row, or None if absent.

    Marks ``source = manual`` since a human touched it.
    """
    tx = session.get(Transaction, tx_id)
    if tx is None:
        return None
    tx.category_id = category_id
    tx.scope = scope
    tx.source = TxSource.manual
    session.add(tx)
    session.commit()

    return tx
