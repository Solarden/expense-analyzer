"""Transfer queries — the write/read side of internal-transfer handling.

The pairing *logic* is pure and lives in :mod:`expense_analyzer.transfers`; this
module is the only place that touches the DB for it: it loads unmatched
candidates, links a confirmed pair (shared ``transfer_group_id`` + the built-in
``Transfer`` category on both legs) and unlinks them again.

A confirmed transfer is marked two ways on purpose (owner's call):
- ``transfer_group_id`` links the two legs and is the signal a later stats phase
  keys on to exclude transfers from spending/income;
- a singleton ``Transfer`` category (``kind=transfer``) is assigned to both legs
  so they read uniformly with normal categorization in the UI.
"""

from itertools import groupby
from uuid import uuid4

from sqlmodel import Session, col, select

from expense_analyzer.models import Category, CategoryKind, Transaction
from expense_analyzer.transfers import DetectionResult, find_transfer_pairs

TRANSFER_CATEGORY_NAME = "Transfer"


def unmatched_candidates(session: Session) -> list[Transaction]:
    """Non-deleted transactions not yet part of a transfer group."""
    return list(
        session.exec(
            select(Transaction).where(
                col(Transaction.deleted_at).is_(None),
                col(Transaction.transfer_group_id).is_(None),
            )
        ).all()
    )


def ensure_transfer_category(session: Session) -> Category:
    """Get-or-create the singleton ``Transfer`` category (``kind=transfer``).

    No DB-level uniqueness on ``kind`` (would need a migration; this phase adds no
    schema). That's safe under the app's single-writer model — imports run from
    the one ``app`` process and the worker is a placeholder — so two concurrent
    creates can't race. If the worker ever writes, revisit with a unique index.
    """
    category = session.exec(select(Category).where(Category.kind == CategoryKind.transfer)).first()
    if category is None:
        category = Category(name=TRANSFER_CATEGORY_NAME, kind=CategoryKind.transfer)
        session.add(category)
        session.commit()
        session.refresh(category)

    return category


def link_transfer(session: Session, *, tx_a_id: int, tx_b_id: int) -> str | None:
    """Link two transactions as one transfer. Returns the group id, or ``None``.

    Returns ``None`` (rather than raising) when the pair is missing or does not
    form a legal transfer — opposite signs, equal absolute amount, different
    accounts, and neither leg already in a group. The caller turns that into a
    404, matching the dashboard's error discipline. Order of the two ids does
    not matter.

    Note: the date window is deliberately **not** enforced here. The window only
    governs *auto*-suggestion (see :func:`expense_analyzer.transfers.find_transfer_pairs`);
    a human confirming a pair may legitimately link two legs that booked further
    apart than the window, so manual confirmation is allowed to override it.
    """
    a = session.get(Transaction, tx_a_id)
    b = session.get(Transaction, tx_b_id)
    if a is None or b is None:
        return None
    if a.deleted_at is not None or b.deleted_at is not None:
        return None
    if a.transfer_group_id is not None or b.transfer_group_id is not None:
        return None
    if a.account_id == b.account_id or a.amount != -b.amount or a.amount == 0:
        return None

    category = ensure_transfer_category(session)
    group_id = uuid4().hex
    for tx in (a, b):
        tx.transfer_group_id = group_id
        tx.category_id = category.id
    session.add_all([a, b])
    session.commit()

    return group_id


def unlink_transfer(session: Session, group_id: str) -> int:
    """Undo a transfer link: clear the group on both legs and drop the Transfer
    category where it points at it. Returns the number of rows touched."""
    rows = session.exec(select(Transaction).where(Transaction.transfer_group_id == group_id)).all()
    if not rows:
        return 0

    transfer_category = session.exec(
        select(Category).where(Category.kind == CategoryKind.transfer)
    ).first()
    transfer_category_id = transfer_category.id if transfer_category else None
    for tx in rows:
        tx.transfer_group_id = None
        if tx.category_id == transfer_category_id:
            tx.category_id = None
    session.add_all(rows)
    session.commit()

    return len(rows)


def list_transfer_groups(session: Session) -> list[list[Transaction]]:
    """Confirmed transfer groups, each a list of its legs, newest first."""
    rows = session.exec(
        select(Transaction)
        .where(
            col(Transaction.deleted_at).is_(None),
            col(Transaction.transfer_group_id).is_not(None),
        )
        .order_by(col(Transaction.transfer_group_id))
    ).all()

    groups = [list(legs) for _, legs in groupby(rows, key=lambda t: t.transfer_group_id)]
    groups.sort(key=lambda legs: max(t.booked_date for t in legs), reverse=True)

    return groups


def detect_and_autolink(session: Session, *, window_days: int) -> tuple[int, DetectionResult]:
    """Run detection over all unmatched candidates, auto-linking the unambiguous
    pairs. Returns ``(auto_linked_count, result)`` so the caller can also surface
    the ambiguous suggestions. Auto pairs are vertex-disjoint (mutual uniqueness),
    so linking them in sequence is safe."""
    result = find_transfer_pairs(unmatched_candidates(session), window_days=window_days)

    linked = 0
    for pair in result.auto:
        if link_transfer(session, tx_a_id=pair.outflow.id, tx_b_id=pair.inflow.id) is not None:
            linked += 1

    return linked, result
