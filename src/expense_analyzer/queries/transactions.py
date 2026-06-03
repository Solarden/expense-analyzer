"""Transaction queries."""

from sqlmodel import Session, col, select

from expense_analyzer.models import Scope, Transaction, TxSource

# Cap the unpaginated list so an old, large DB can't render thousands of rows.
# Full pagination lands with the Phase 4 dashboard (roadmap §11).
DEFAULT_LIMIT = 500


def list_transactions(
    session: Session,
    account_id: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[Transaction]:
    """Non-deleted transactions, newest first, optionally filtered to one account."""
    query = select(Transaction).where(col(Transaction.deleted_at).is_(None))
    if account_id is not None:
        query = query.where(Transaction.account_id == account_id)
    query = query.order_by(col(Transaction.booked_date).desc(), col(Transaction.id).desc()).limit(
        limit
    )

    return list(session.exec(query).all())


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
