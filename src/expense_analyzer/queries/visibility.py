"""The per-viewer visibility boundary for transactions — the segregation feature's
one security invariant, applied at every read that renders or sums transactions.

A viewer sees a row iff it is ``household`` scope **or** it is their own
``private`` row. Another member's private rows appear in no list, total, chart, or
export.

The lens narrows *within* that visible set; it can never widen past it:

- ``all``     -> my private + all household  (the default landing view)
- ``private`` -> only my own private rows
- ``home``    -> only household rows (the shared home budget), any owner

SECURITY DEFAULT: a call with ``viewer_id=None`` (background jobs, e.g. the Home
Assistant export) collapses to household-only, so a forgotten viewer can never
leak a private row.
"""

from sqlalchemy import and_, false, or_
from sqlmodel.sql.expression import SelectOfScalar

from expense_analyzer.models import Lens, Scope, Transaction


def visible_to(
    query: SelectOfScalar, *, viewer_id: int | None, lens: Lens = Lens.all
) -> SelectOfScalar:
    """AND a single grouped visibility clause onto ``query`` (see module docstring).

    One grouped ``or_(...)`` so it composes correctly with any other ``OR`` already
    on the query (e.g. the transaction-list search)."""
    household = Transaction.scope == Scope.household
    if lens is Lens.home:
        return query.where(household)
    if viewer_id is None:
        # No viewer: only ``all`` is meaningful, and it collapses to household.
        return query.where(household if lens is Lens.all else false())
    mine = and_(Transaction.scope == Scope.private, Transaction.owner_id == viewer_id)

    return query.where(mine if lens is Lens.private else or_(household, mine))


def resolve_lens(raw: str | None) -> Lens:
    """Parse a ``?lens=`` value leniently; anything unknown -> the safe default."""
    try:
        return Lens(raw) if raw is not None else Lens.all
    except ValueError:
        return Lens.all
