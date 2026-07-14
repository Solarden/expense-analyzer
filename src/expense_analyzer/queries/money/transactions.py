"""Transaction queries — the read/write side of the transaction list.

The list is filtered and paginated (Phase 4): the old unpaginated 500-row cap is
gone, replaced by ``page_size`` windows so an old, large DB renders one cheap
page at a time. Filters and pagination share one ``_apply_filters`` builder so
the page query and its ``COUNT`` can never drift apart.
"""

import re
from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from sqlalchemy import func
from sqlmodel import Session, col, select
from sqlmodel.sql.expression import SelectOfScalar

from expense_analyzer.clock import utc_now
from expense_analyzer.importers.merchant import normalize_merchant
from expense_analyzer.models import ImportBatch, ImportStatus, Lens, Scope, Transaction, TxSource
from expense_analyzer.queries.visibility import visible_to

# Sentinel for the category filter: match rows with no category at all.
UNCATEGORIZED = "none"

# Manual (hand-entered) transactions all live in one reuse-or-create batch — the
# batch is just the NOT-NULL container ``import_batch_id`` requires, never a unit
# you'd bulk-roll-back. A row's membership in this batch (not its ``source``, which
# becomes ``manual`` the moment anyone categorizes an *imported* row) is what marks
# it as a hand-entered, fully-editable, individually-deletable transaction.
MANUAL_BATCH_SOURCE = "Manual"
MANUAL_BATCH_FILENAME = "(manual entries)"

_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


@dataclass(frozen=True)
class TransactionFilters:
    account_id: int | None = None
    month: str | None = None  # "YYYY-MM"
    category_id: int | None = None  # set with uncategorized=False
    uncategorized: bool = False  # category_id IS NULL
    scope: Scope | None = None
    owner_id: int | None = None  # rows added by this member (set with unowned=False)
    unowned: bool = False  # owner_id IS NULL (rows added by a departed member)
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
    query: SelectOfScalar[Transaction],
    filters: TransactionFilters,
    *,
    viewer_id: int | None,
    lens: Lens = Lens.all,
) -> SelectOfScalar[Transaction]:
    query = query.where(col(Transaction.deleted_at).is_(None))
    # Per-viewer visibility — applied here (not in list_transactions) so the page
    # query and its COUNT share the exact clause and can never drift.
    query = visible_to(query, viewer_id=viewer_id, lens=lens)
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
    # "Added by" filter — mirrors the category/uncategorized pair. Applied AFTER
    # visible_to, so it can only narrow the already-visible set: a crafted
    # ?added_by=<other member> can never surface that member's private rows.
    if filters.unowned:
        query = query.where(col(Transaction.owner_id).is_(None))
    elif filters.owner_id is not None:
        query = query.where(Transaction.owner_id == filters.owner_id)
    if filters.scope is not None:
        query = query.where(Transaction.scope == filters.scope)
    if filters.search and filters.search.strip():
        # Escape LIKE wildcards so a literal % or _ in the search box doesn't
        # silently widen the match. NOTE: case-insensitivity is dialect-bound —
        # PostgreSQL ILIKE case-folds Unicode (Polish Ł/Ą… match), while SQLite
        # (dev) lowercases ASCII only. Prod gets the better behavior.
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
    viewer_id: int | None = None,
    lens: Lens = Lens.all,
) -> TransactionPage:
    """One page of non-deleted transactions the viewer may see (newest first)
    matching ``filters``."""
    page = max(1, page)

    total = session.exec(
        _apply_filters(
            select(func.count()).select_from(Transaction),  # type: ignore[arg-type]
            filters,
            viewer_id=viewer_id,
            lens=lens,
        )
    ).one()

    rows_query = _apply_filters(select(Transaction), filters, viewer_id=viewer_id, lens=lens)
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


def _get_visible(session: Session, tx_id: int, *, viewer_id: int | None) -> Transaction | None:
    """Load a live transaction only if ``viewer_id`` may see it — else None.

    Mirrors :func:`expense_analyzer.queries.visibility.visible_to` for a single row,
    so a crafted ``/transactions/{id}/...`` request cannot read or write another
    member's private row (IDOR).
    """
    tx = session.get(Transaction, tx_id)
    if tx is None or tx.deleted_at is not None:
        return None
    if tx.scope == Scope.private and tx.owner_id != viewer_id:
        return None

    return tx


def set_category(
    session: Session,
    *,
    tx_id: int,
    category_id: int | None,
    scope: Scope,
    viewer_id: int | None = None,
) -> Transaction | None:
    """Manually (re)categorize a transaction the viewer may see. Returns the row, or
    None if absent / not visible to ``viewer_id`` (IDOR guard).

    Marks ``source = manual`` since a human touched it. Marking a row ``private``
    stamps ``owner_id`` to the viewer, so it can never become owner-less (which
    would hide it from everyone).
    """
    tx = _get_visible(session, tx_id, viewer_id=viewer_id)
    if tx is None:
        return None
    tx.category_id = category_id
    tx.scope = scope
    if scope == Scope.private and viewer_id is not None:
        tx.owner_id = viewer_id
    tx.source = TxSource.manual
    session.add(tx)
    session.commit()

    return tx


def get_transaction(
    session: Session, tx_id: int, *, viewer_id: int | None = None
) -> Transaction | None:
    """Load a single transaction the viewer may see (the edit form's subject)."""
    return _get_visible(session, tx_id, viewer_id=viewer_id)


def ensure_manual_batch(session: Session) -> ImportBatch:
    """Find (or lazily create) the single active batch that owns manual entries."""
    batch = session.exec(
        select(ImportBatch).where(
            ImportBatch.source == MANUAL_BATCH_SOURCE,
            ImportBatch.status == ImportStatus.active,
        )
    ).first()
    if batch is None:
        batch = ImportBatch(
            source=MANUAL_BATCH_SOURCE,
            filename=MANUAL_BATCH_FILENAME,
            record_count=0,
            status=ImportStatus.active,
        )
        session.add(batch)
        session.flush()  # assign batch.id

    return batch


def is_manual_entry(session: Session, tx: Transaction) -> bool:
    """True if ``tx`` was hand-entered (lives in the Manual batch) — and so may be
    fully edited and individually deleted, unlike a bank-imported row."""
    batch = session.get(ImportBatch, tx.import_batch_id)

    return batch is not None and batch.source == MANUAL_BATCH_SOURCE


def manual_batch_id(session: Session) -> int | None:
    """The active Manual batch's id, or ``None`` if no manual entry exists yet.

    Read-only counterpart to :func:`ensure_manual_batch` (never creates a row): for
    classifying a whole list of rows in one query — compare ``tx.import_batch_id``
    against it — instead of a per-row :func:`is_manual_entry` lookup."""
    return session.exec(
        select(col(ImportBatch.id)).where(
            ImportBatch.source == MANUAL_BATCH_SOURCE,
            ImportBatch.status == ImportStatus.active,
        )
    ).first()


def create_manual_transaction(
    session: Session,
    *,
    account_id: int,
    booked_date: date,
    amount: int,
    description: str,
    category_id: int | None,
    scope: Scope,
    note: str | None,
    owner_id: int | None,
) -> Transaction:
    """Hand-enter a transaction (mainly cash — no CSV path otherwise).

    ``fingerprint`` is a fresh uuid, **not** a content hash: two identical cash
    operations (e.g. two 20 zł coffees on the same day) are genuinely distinct, so
    content-based dedup would be wrong here. ``source = manual``.
    """
    batch = ensure_manual_batch(session)
    tx = Transaction(
        account_id=account_id,
        import_batch_id=batch.id,
        amount=amount,
        booked_date=booked_date,
        raw_description=description,
        merchant_normalized=normalize_merchant(description),
        note=note,
        category_id=category_id,
        scope=scope,
        owner_id=owner_id,
        source=TxSource.manual,
        fingerprint=uuid4().hex,
    )
    session.add(tx)
    batch.record_count += 1
    session.add(batch)
    session.commit()
    session.refresh(tx)

    return tx


def update_transaction(
    session: Session,
    *,
    tx_id: int,
    viewer_id: int | None = None,
    category_id: int | None,
    scope: Scope,
    note: str | None,
    account_id: int | None = None,
    booked_date: date | None = None,
    amount: int | None = None,
    description: str | None = None,
) -> Transaction | None:
    """Edit a transaction from the edit form. Returns the row, or None if absent.

    Category, scope and note are editable on every row. The bank-sourced fields
    (account/date/amount/description) are only rewritten when the caller passes
    them — the endpoint does so **only for manual entries**, since an imported
    row's amount/date/description are the bank's source of truth and feed its
    import fingerprint. A human touched it, so ``source = manual``.
    """
    tx = _get_visible(session, tx_id, viewer_id=viewer_id)
    if tx is None:
        return None
    # Flag a human (re)categorization only when the category or scope actually
    # changed — editing just the note must not flip ``source`` to manual, which
    # would wrongly shield the row from future rule re-categorization.
    if tx.category_id != category_id or tx.scope != scope:
        tx.source = TxSource.manual
    tx.category_id = category_id
    tx.scope = scope
    if scope == Scope.private and viewer_id is not None:
        tx.owner_id = viewer_id
    tx.note = note
    if account_id is not None:
        tx.account_id = account_id
    if booked_date is not None:
        tx.booked_date = booked_date
    if amount is not None:
        tx.amount = amount
    if description is not None:
        tx.raw_description = description
        tx.merchant_normalized = normalize_merchant(description)
    session.add(tx)
    session.commit()
    session.refresh(tx)

    return tx


def set_note(
    session: Session, *, tx_id: int, note: str | None, viewer_id: int | None = None
) -> Transaction | None:
    """Set (or clear) a transaction's free-text note. Returns the row, or None if
    absent / deleted.

    A note is an annotation, not categorization — so this deliberately does **not**
    touch ``source`` (unlike :func:`set_category` / :func:`update_transaction`). It
    works on any row, imported or manual.
    """
    tx = _get_visible(session, tx_id, viewer_id=viewer_id)
    if tx is None:
        return None
    tx.note = note
    session.add(tx)
    session.commit()
    session.refresh(tx)

    return tx


def soft_delete_transaction(
    session: Session, *, tx_id: int, viewer_id: int | None = None
) -> Transaction | None:
    """Soft-delete a transaction (set ``deleted_at``). Returns the row, or None if
    absent / already deleted. Caller gates this to manual entries — imported rows
    are removed by rolling back their import batch, not one at a time."""
    tx = _get_visible(session, tx_id, viewer_id=viewer_id)
    if tx is None:
        return None
    tx.deleted_at = utc_now()
    session.add(tx)
    # Keep the owning batch's record_count (the home page's "Records" column, and
    # what a rollback would remove) in step with the soft delete.
    batch = session.get(ImportBatch, tx.import_batch_id)
    if batch is not None and batch.record_count > 0:
        batch.record_count -= 1
        session.add(batch)
    session.commit()

    return tx
