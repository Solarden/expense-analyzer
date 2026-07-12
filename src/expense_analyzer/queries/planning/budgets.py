"""Budget queries — per-category monthly limits and how they compare to spending.

The DB side of Phase 8 (design §7.6). A :class:`~expense_analyzer.models.Budget`
is either a **recurring** default (``month is None``, applies every month) or a
**one-off override** for a single ``"YYYY-MM"`` that wins for that month only.

:func:`set_budget` is an upsert (find-or-update by category + month), so the app —
the single writer — never creates a duplicate, including a second recurring row
that SQLite's NULL-distinct unique constraint wouldn't catch (see the
:class:`~expense_analyzer.models.Budget` docstring).

:func:`budget_overview` joins the effective limits against the month's actual
spending. Spending comes from :mod:`expense_analyzer.queries.money.stats`, so transfers
**and** loan installment payments are already excluded — a budget tracks real
consumption, not money moved between own accounts or debt repayment.
"""

from dataclasses import dataclass

from sqlalchemy import or_
from sqlmodel import Session, col, select

from expense_analyzer.models import Budget, Category, CategoryKind, Lens, Scope, Transaction
from expense_analyzer.queries.money import stats


def list_budgets(
    session: Session, *, scope: Scope | None = None, viewer_id: int | None = None
) -> list[Budget]:
    """Budgets the ``viewer`` may see (optionally just one ``scope``), recurring
    defaults first then overrides, by category id.

    Visibility mirrors transactions: a household budget is shared; a private budget
    is only its owner's. ``viewer_id=None`` (the default) sees household only — the
    safe default for ambient/background callers, matching ``visible_to``."""
    stmt = select(Budget).order_by(col(Budget.category_id), col(Budget.month).nulls_first())
    if scope is not None:
        stmt = stmt.where(Budget.scope == scope)
    if viewer_id is None:
        stmt = stmt.where(Budget.scope == Scope.household)
    else:
        stmt = stmt.where(or_(Budget.scope == Scope.household, Budget.owner_id == viewer_id))

    return list(session.exec(stmt).all())


def _get_visible_budget(
    session: Session, budget_id: int, *, viewer_id: int | None
) -> Budget | None:
    """A budget only if ``viewer_id`` may see it (household, or its own private) —
    else None. The single-row IDOR gate for the edit/delete endpoints, mirroring
    ``queries.money.transactions._get_visible``."""
    budget = session.get(Budget, budget_id)
    if budget is None:
        return None
    if budget.scope is Scope.private and budget.owner_id != viewer_id:
        return None

    return budget


def get_budget(session: Session, budget_id: int, *, viewer_id: int | None = None) -> Budget | None:
    """A budget by id, gated to the viewer (household or own private) — the edit path."""
    return _get_visible_budget(session, budget_id, viewer_id=viewer_id)


def set_budget(
    session: Session,
    *,
    category_id: int,
    month: str | None,
    limit_amount: int,
    scope: Scope = Scope.household,
    viewer_id: int | None = None,
) -> Budget:
    """Create or update the budget for ``(category_id, month, scope, owner)``.

    ``month`` is ``None`` for the recurring default or a ``"YYYY-MM"`` override;
    ``scope`` separates a member's private limits from the shared household ones. A
    **private** budget belongs to ``viewer_id`` (its owner); a **household** budget
    is shared (``owner_id`` NULL). The owner is part of the upsert key, so two
    members can each hold their own private limit for the same category/month.
    Re-setting an existing slot updates its limit rather than inserting a second
    row (the single-writer guard against duplicates — see module docstring).
    """
    owner_id = viewer_id if scope is Scope.private else None
    stmt = select(Budget).where(
        Budget.category_id == category_id,
        Budget.scope == scope,
        col(Budget.owner_id).is_(None) if owner_id is None else Budget.owner_id == owner_id,
    )
    stmt = (
        stmt.where(col(Budget.month).is_(None))
        if month is None
        else stmt.where(Budget.month == month)
    )
    budget = session.exec(stmt).first()

    if budget is None:
        budget = Budget(
            category_id=category_id,
            month=month,
            limit_amount=limit_amount,
            scope=scope,
            owner_id=owner_id,
        )
    else:
        budget.limit_amount = limit_amount
    session.add(budget)
    session.commit()
    session.refresh(budget)

    return budget


def delete_budget(session: Session, budget_id: int, *, viewer_id: int | None = None) -> bool:
    """Delete a budget the ``viewer`` may see. Returns False if it doesn't exist or
    the viewer may not see it (another member's private budget — IDOR gate).

    Budgets are config, not financial records (like loans, unlike transactions) —
    a wrong limit is just re-entered, so this is a hard delete.
    """
    budget = _get_visible_budget(session, budget_id, viewer_id=viewer_id)
    if budget is None:
        return False

    session.delete(budget)
    session.commit()

    return True


@dataclass(frozen=True)
class EffectiveLimit:
    """The limit in force for one category in a given month, and where it came from."""

    limit_amount: int
    is_override: bool  # True: a "YYYY-MM" override; False: the recurring default


def effective_limits(budgets: list[Budget], month: str) -> dict[int, EffectiveLimit]:
    """Resolve each category's limit for ``month``: a month override wins over the
    recurring default. Pure over a preloaded budget list."""
    recurring = {b.category_id: b.limit_amount for b in budgets if b.month is None}
    overrides = {b.category_id: b.limit_amount for b in budgets if b.month == month}

    limits: dict[int, EffectiveLimit] = {}
    for category_id in recurring.keys() | overrides.keys():
        if category_id in overrides:
            limits[category_id] = EffectiveLimit(overrides[category_id], is_override=True)
        else:
            limits[category_id] = EffectiveLimit(recurring[category_id], is_override=False)

    return limits


@dataclass(frozen=True)
class BudgetStatus:
    """A budgeted category's limit vs actual spending for one month."""

    category_id: int
    name: str
    limit_amount: int  # effective limit (minor units)
    spent: int  # positive magnitude spent this month (transfers/loans excluded)
    is_override: bool  # the limit is a month override, not the recurring default

    @property
    def remaining(self) -> int:
        """Limit minus spending. Negative once the budget is exceeded."""
        return self.limit_amount - self.spent

    @property
    def over(self) -> bool:
        return self.spent > self.limit_amount

    @property
    def pct(self) -> int:
        """Share of the limit consumed, clamped to 0–100 for a progress bar.

        A zero/negative limit reads as 100% the moment anything is spent (the
        bar is "full"); ``remaining`` still carries the exact signed figure.
        """
        if self.limit_amount <= 0:
            return 100 if self.spent > 0 else 0

        return min(100, self.spent * 100 // self.limit_amount)

    @property
    def pct_full(self) -> int:
        """True share of the limit consumed, **uncapped** — for the % label, which
        keeps counting past 100 once over (e.g. 180%). The progress bar width still
        uses the clamped :attr:`pct`. Mirrors ``pct`` for a non-positive limit."""
        if self.limit_amount <= 0:
            return 100 if self.spent > 0 else 0

        return self.spent * 100 // self.limit_amount


def budget_overview(
    session: Session,
    month: str,
    *,
    scope: Scope = Scope.household,
    viewer_id: int | None = None,
    spendable: list[Transaction] | None = None,
) -> list[BudgetStatus]:
    """Every budgeted category's limit vs its actual spending in ``month``.

    Returns one :class:`BudgetStatus` per category that has a recurring default or
    a ``month`` override, sorted by category name. Empty when no budgets exist, so
    callers can render an empty state without a special case.

    ``spent`` is **gross** expense, matching :func:`stats.month_summary`: a refund
    booked in an expense category counts as income (a positive amount), so it does
    **not** offset the category's spending. Deliberate — keeps a budget's "Spent"
    consistent with the Overview's per-category figure (one source of truth) rather
    than netting only here. Revisit if real data shows in-category refunds matter.

    Pass ``spendable`` (a preloaded :func:`stats.spendable_transactions`) to reuse
    a scan the caller already did — e.g. the HA metrics collector, which needs the
    month figures anyway. Omit it for a self-contained single scan.
    """
    limits = effective_limits(list_budgets(session, scope=scope, viewer_id=viewer_id), month)
    if not limits:
        return []

    category_names = {
        c.id: c.name for c in session.exec(select(Category)).all() if c.id is not None
    }
    if spendable is None:
        # Spend must match the limits' scope: household budgets track household
        # spend; private budgets track the viewer's own private spend.
        spend_lens = Lens.home if scope is Scope.household else Lens.private
        spendable = stats.spendable_transactions(session, viewer_id=viewer_id, lens=spend_lens)
    # Transfer/loan-excluded month spend per category (the pure summary is cheap;
    # the DB scan it walks is what `spendable` lets the caller share).
    summary = stats.month_summary(spendable, month, category_names)
    spent_by_category = {c.category_id: c.total for c in summary.by_category}

    statuses = [
        BudgetStatus(
            category_id=category_id,
            name=category_names.get(category_id, f"#{category_id}"),
            limit_amount=eff.limit_amount,
            spent=spent_by_category.get(category_id, 0),
            is_override=eff.is_override,
        )
        for category_id, eff in limits.items()
    ]
    statuses.sort(key=lambda s: s.name.lower())

    return statuses


def budgetable_categories(session: Session) -> list[Category]:
    """Expense categories — the only ones a spending budget makes sense for.

    Income and ``transfer`` categories are excluded from the budget form: a limit
    on income or on internal transfers is meaningless.
    """
    return list(
        session.exec(
            select(Category)
            .where(Category.kind == CategoryKind.expense)
            .order_by(col(Category.name))
        ).all()
    )
