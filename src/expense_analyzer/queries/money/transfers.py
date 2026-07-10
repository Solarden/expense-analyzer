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
from expense_analyzer.queries.money.transactions import _get_visible
from expense_analyzer.queries.visibility import visible_to
from expense_analyzer.transfers import DetectionResult, find_transfer_pairs

TRANSFER_CATEGORY_NAME = "Transfer"


def unmatched_candidates(session: Session, *, viewer_id: int | None = None) -> list[Transaction]:
    """Non-deleted transactions the viewer may see that aren't yet in a transfer
    group. Viewer-scoped so another member's private legs are never offered as a
    transfer candidate (``viewer_id=None`` → household-only)."""
    query = visible_to(
        select(Transaction).where(
            col(Transaction.deleted_at).is_(None),
            col(Transaction.transfer_group_id).is_(None),
        ),
        viewer_id=viewer_id,
    )

    return list(session.exec(query).all())


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


def link_transfer(
    session: Session, *, tx_a_id: int, tx_b_id: int, viewer_id: int | None = None
) -> str | None:
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
    # _get_visible enforces the privacy boundary: a leg the viewer may not see
    # (another member's private row) reads as absent, so it can't be linked (IDOR).
    a = _get_visible(session, tx_a_id, viewer_id=viewer_id)
    b = _get_visible(session, tx_b_id, viewer_id=viewer_id)
    if a is None or b is None:
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


def unlink_transfer(session: Session, group_id: str, *, viewer_id: int | None = None) -> int:
    """Undo a transfer link: clear the group on both legs and drop the Transfer
    category where it points at it. Returns the number of rows touched. Viewer-scoped
    so a member can't unlink a group they can't see (another member's private legs)."""
    rows = session.exec(
        visible_to(
            select(Transaction).where(Transaction.transfer_group_id == group_id),
            viewer_id=viewer_id,
        )
    ).all()
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


def list_transfer_groups(
    session: Session, *, viewer_id: int | None = None
) -> list[list[Transaction]]:
    """Confirmed transfer groups the viewer may see, each a list of its legs, newest
    first (``viewer_id=None`` → household-only)."""
    rows = session.exec(
        visible_to(
            select(Transaction).where(
                col(Transaction.deleted_at).is_(None),
                col(Transaction.transfer_group_id).is_not(None),
            ),
            viewer_id=viewer_id,
        ).order_by(col(Transaction.transfer_group_id))
    ).all()

    groups = [list(legs) for _, legs in groupby(rows, key=lambda t: t.transfer_group_id)]
    groups.sort(key=lambda legs: max(t.booked_date for t in legs), reverse=True)

    return groups


def detect_and_autolink(
    session: Session, *, window_days: int, viewer_id: int | None = None
) -> tuple[int, DetectionResult]:
    """Run detection over the viewer's unmatched candidates, auto-linking the
    unambiguous pairs. Returns ``(auto_linked_count, result)`` so the caller can also
    surface the ambiguous suggestions. Auto pairs are vertex-disjoint (mutual
    uniqueness), so linking them in sequence is safe. At import ``viewer_id`` is the
    uploading user, so their own private transfers still auto-link."""
    result = find_transfer_pairs(
        unmatched_candidates(session, viewer_id=viewer_id), window_days=window_days
    )

    linked = 0
    for pair in result.auto:
        group = link_transfer(
            session, tx_a_id=pair.outflow.id, tx_b_id=pair.inflow.id, viewer_id=viewer_id
        )
        if group is not None:
            linked += 1

    return linked, result
