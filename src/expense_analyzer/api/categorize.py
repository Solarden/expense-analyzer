"""Shared categorization helper for the dashboard handlers.

Both the transactions list and the review queue let a user (re)assign a
transaction's category from the same :class:`~expense_analyzer.api.forms.CategorizeForm`.
The form parsing, validation and not-found handling are identical — only where each
handler redirects afterwards differs — so the shared part lives here, keeping the
two handlers to "apply, then redirect". It belongs in the API layer (it raises
``HTTPException``), leaving the ``queries`` layer web-agnostic.
"""

from fastapi import HTTPException, status
from sqlmodel import Session

from expense_analyzer.models import Scope
from expense_analyzer.queries import categories as category_queries
from expense_analyzer.queries import transactions as transaction_queries


def parse_category_id(session: Session, raw_category_id: str) -> int | None:
    """Validate a form's category value into a category id (or None for cleared).

    ``raw_category_id`` is a digit string (selects that category) or ``""`` (clears
    it — uncategorized). Raises 400 on a non-numeric id, 404 on an unknown category.
    Shared by every handler that takes a category off a form.
    """
    if not raw_category_id:
        return None
    if not raw_category_id.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid category id: {raw_category_id!r}",
        )
    category_id = int(raw_category_id)
    if category_queries.get_category(session, category_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"category {category_id} not found",
        )

    return category_id


def apply_categorization(
    session: Session, *, tx_id: int, raw_category_id: str, scope: Scope
) -> None:
    """Validate a categorize-form submission and apply it (``source = manual``).

    ``raw_category_id`` is the form's category value: a digit string selects that
    category, ``""`` clears it (uncategorized). Raises 400 on a non-numeric id, 404
    on an unknown category or a missing transaction.
    """
    category_id = parse_category_id(session, raw_category_id)

    if (
        transaction_queries.set_category(session, tx_id=tx_id, category_id=category_id, scope=scope)
        is None
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"transaction {tx_id} not found"
        )
