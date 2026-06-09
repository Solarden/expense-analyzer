"""Budgets (Phase 8): pure helpers and the query layer.

Pure helpers (``effective_limits``, ``BudgetStatus`` properties) run on plain
objects; the query-layer tests run on ``db_session`` with conftest builders. The
key behaviours: a month override beats the recurring default, the upsert never
duplicates a slot, and budget spending excludes transfers *and* loan installments
(the same exclusions ``queries.stats`` applies).
"""

from collections.abc import Callable
from datetime import date

from sqlmodel import Session

from expense_analyzer.models import (
    Account,
    AccountType,
    Budget,
    Category,
    CategoryKind,
    Loan,
    Transaction,
)
from expense_analyzer.queries.planning import budgets as bq

# --- pure helpers ----------------------------------------------------------


def test_effective_limits_month_override_beats_recurring() -> None:
    budgets = [
        Budget(category_id=1, month=None, limit_amount=200_00),
        Budget(category_id=1, month="2026-06", limit_amount=300_00),
        Budget(category_id=2, month=None, limit_amount=50_00),
    ]

    limits = bq.effective_limits(budgets, "2026-06")

    assert limits[1].limit_amount == 300_00
    assert limits[1].is_override is True
    # Category 2 has only a recurring default — that's what applies.
    assert limits[2].limit_amount == 50_00
    assert limits[2].is_override is False


def test_effective_limits_falls_back_to_recurring_for_other_months() -> None:
    budgets = [
        Budget(category_id=1, month=None, limit_amount=200_00),
        Budget(category_id=1, month="2026-05", limit_amount=999_00),  # a different month
    ]

    limits = bq.effective_limits(budgets, "2026-06")

    assert limits[1].limit_amount == 200_00
    assert limits[1].is_override is False


def test_budget_status_remaining_over_and_clamped_pct() -> None:
    under = bq.BudgetStatus(
        category_id=1, name="Food", limit_amount=200_00, spent=150_00, is_override=False
    )
    assert under.remaining == 50_00
    assert under.over is False
    assert under.pct == 75
    assert under.pct_full == 75  # under budget: label matches the bar

    over = bq.BudgetStatus(
        category_id=1, name="Food", limit_amount=200_00, spent=250_00, is_override=False
    )
    assert over.remaining == -50_00
    assert over.over is True
    assert over.pct == 100  # bar clamped, never past full
    assert over.pct_full == 125  # label keeps counting past 100% when over

    zero_limit = bq.BudgetStatus(
        category_id=1, name="X", limit_amount=0, spent=10_00, is_override=False
    )
    assert zero_limit.pct == 100  # any spend against a zero limit reads as full
    assert zero_limit.pct_full == 100


# --- query layer -----------------------------------------------------------


def test_set_budget_upserts_recurring_slot(
    db_session: Session, make_category: Callable[..., Category]
) -> None:
    cat = make_category(name="Food", kind=CategoryKind.expense)
    bq.set_budget(db_session, category_id=cat.id, month=None, limit_amount=200_00)
    bq.set_budget(db_session, category_id=cat.id, month=None, limit_amount=250_00)

    budgets = bq.list_budgets(db_session)
    assert len(budgets) == 1  # second call updated, not inserted
    assert budgets[0].limit_amount == 250_00


def test_set_budget_recurring_and_override_coexist(
    db_session: Session, make_category: Callable[..., Category]
) -> None:
    cat = make_category(name="Food", kind=CategoryKind.expense)
    bq.set_budget(db_session, category_id=cat.id, month=None, limit_amount=200_00)
    bq.set_budget(db_session, category_id=cat.id, month="2026-06", limit_amount=300_00)

    budgets = bq.list_budgets(db_session)
    assert len(budgets) == 2
    # Recurring (month NULL) sorts first via nulls_first().
    assert budgets[0].month is None
    assert budgets[1].month == "2026-06"


def test_delete_budget(db_session: Session, make_category: Callable[..., Category]) -> None:
    cat = make_category(kind=CategoryKind.expense)
    budget = bq.set_budget(db_session, category_id=cat.id, month=None, limit_amount=100_00)

    assert bq.delete_budget(db_session, budget.id) is True
    assert bq.list_budgets(db_session) == []
    assert bq.delete_budget(db_session, 999) is False  # already gone / never existed


def test_budgetable_categories_only_expense(
    db_session: Session, make_category: Callable[..., Category]
) -> None:
    make_category(name="Food", kind=CategoryKind.expense)
    make_category(name="Salary", kind=CategoryKind.income)
    make_category(name="Transfer", kind=CategoryKind.transfer)

    names = [c.name for c in bq.budgetable_categories(db_session)]
    assert names == ["Food"]  # income/transfer excluded


def test_budget_overview_joins_spending_excluding_transfers_and_loans(
    db_session: Session,
    make_account: Callable[..., Account],
    make_category: Callable[..., Category],
    make_loan: Callable[..., Loan],
    make_transaction: Callable[..., Transaction],
) -> None:
    account = make_account(name="PKO checking")
    loan_acc = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=loan_acc.id)
    food = make_category(name="Food", kind=CategoryKind.expense)
    bq.set_budget(db_session, category_id=food.id, month=None, limit_amount=200_00)

    booked = date(2026, 6, 10)
    make_transaction(account_id=account.id, amount=-120_00, booked_date=booked, category_id=food.id)
    # A loan installment tagged Food must NOT count toward the Food budget.
    make_transaction(
        account_id=account.id,
        amount=-2000_00,
        booked_date=booked,
        category_id=food.id,
        loan_id=loan.id,
        loan_installment_index=1,
    )
    # A transfer leg tagged Food must NOT count either.
    make_transaction(
        account_id=account.id,
        amount=-50_00,
        booked_date=booked,
        category_id=food.id,
        transfer_group_id="grp-1",
    )

    [status] = bq.budget_overview(db_session, "2026-06")
    assert status.name == "Food"
    assert status.spent == 120_00  # only the real spend, not the installment/transfer
    assert status.remaining == 80_00
    assert status.over is False


def test_budget_overview_empty_when_no_budgets(db_session: Session) -> None:
    assert bq.budget_overview(db_session, "2026-06") == []
