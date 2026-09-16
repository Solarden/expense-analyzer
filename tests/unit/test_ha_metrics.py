"""Home Assistant metric collection.

Money is converted to a display decimal string at this edge; transfers stay
excluded from the spending/income figures (consistent with queries/stats).
"""

from collections.abc import Callable

from sqlmodel import Session

from expense_analyzer.clock import local_today
from expense_analyzer.ha.metrics import collect_member_metrics, collect_metrics
from expense_analyzer.models import (
    Account,
    AccountType,
    Budget,
    Category,
    CategoryKind,
    Loan,
    Scope,
    Transaction,
)
from expense_analyzer.queries.core import users

_MONTH_KEYS = ("month_spending", "month_income", "month_net")


def test_empty_database_still_yields_headline_metrics(db_session: Session) -> None:
    metrics = {m.key: m for m in collect_metrics(db_session)}

    # The headline sensors exist even with no data, all reading zero.
    assert metrics["net_worth"].value == "0.00"
    assert metrics["month_spending"].value == "0.00"
    assert metrics["month_income"].value == "0.00"
    assert metrics["month_net"].value == "0.00"
    assert metrics["fixed_monthly_costs"].value == "0.00"


def test_fixed_monthly_costs_sensor(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
) -> None:
    from datetime import date

    account = make_account(name="PKO checking")

    for month in (3, 4, 5):
        make_transaction(
            account_id=account.id,
            amount=-29_99,
            booked_date=date(2026, month, 15),
            merchant_normalized="NETFLIX",
        )

    metrics = {m.key: m.value for m in collect_metrics(db_session)}

    assert metrics["fixed_monthly_costs"] == "29.99"


def test_month_figures_exclude_transfers(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
) -> None:
    account = make_account(name="PKO checking")
    today = local_today()
    make_transaction(account_id=account.id, amount=-100_00, booked_date=today)  # spending 100
    make_transaction(account_id=account.id, amount=500_00, booked_date=today)  # income 500
    # A transfer leg: counts toward the account balance, but NOT spending/income.
    make_transaction(
        account_id=account.id, amount=-200_00, booked_date=today, transfer_group_id="grp-1"
    )

    metrics = {m.key: m.value for m in collect_metrics(db_session)}

    assert metrics["month_spending"] == "100.00"  # the transfer's -200 is not spending
    assert metrics["month_income"] == "500.00"
    assert metrics["month_net"] == "400.00"
    # The per-account balance is the raw sum of every live row (transfer included).
    assert metrics[f"account_{account.id}_balance"] == "200.00"

    # One bank account, no loans/portfolio -> net worth equals that balance.
    assert metrics["net_worth"] == "200.00"


def test_loan_installment_excluded_from_month_spending(
    db_session: Session,
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
    make_transaction: Callable[..., Transaction],
) -> None:
    account = make_account(name="PKO checking")
    loan_account = make_account(name="Mortgage", type=AccountType.loan)
    loan = make_loan(account_id=loan_account.id)
    today = local_today()
    make_transaction(account_id=account.id, amount=-100_00, booked_date=today)  # real spending
    # A loan installment paid from checking: linked to the loan -> not "spending".
    make_transaction(
        account_id=account.id,
        amount=-2000_00,
        booked_date=today,
        loan_id=loan.id,
        loan_installment_index=1,
    )

    metrics = {m.key: m.value for m in collect_metrics(db_session)}

    assert metrics["month_spending"] == "100.00"  # the 2000 installment is excluded

    # ...but it still left the account, so the balance reflects it.
    assert metrics[f"account_{account.id}_balance"] == "-2100.00"


def test_budget_remaining_sensor_per_budgeted_category(
    db_session: Session,
    make_account: Callable[..., Account],
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
    make_budget: Callable[..., Budget],
) -> None:
    account = make_account()
    food = make_category(name="Food", kind=CategoryKind.expense)
    today = local_today()
    make_transaction(account_id=account.id, amount=-120_00, booked_date=today, category_id=food.id)
    make_budget(category_id=food.id, month=today.strftime("%Y-%m"), limit_amount=200_00)

    sensor = next(m for m in collect_metrics(db_session) if m.key == f"budget_{food.id}_remaining")

    assert sensor.name == "Food Budget Remaining"
    assert sensor.value == "80.00"  # 200 limit - 120 spent


def test_per_account_balance_sensor_named_after_account(
    db_session: Session,
    make_account: Callable[..., Account],
) -> None:
    loan_account = make_account(name="Mortgage", type=AccountType.loan)

    metric = next(
        m for m in collect_metrics(db_session) if m.key == f"account_{loan_account.id}_balance"
    )

    assert metric.name == "Mortgage Balance"

    # No loan defined yet -> outstanding 0 -> balance reads zero, not a crash.
    assert metric.value == "0.00"


def test_member_metrics_add_each_members_own_view(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
) -> None:
    """A member's sensor answers "mine and the house's"; the unprefixed one stays the
    house alone, so a dashboard built on it keeps reading the same number."""
    alice = users.create_user(db_session, username="alice", name="Alice", password="pw")
    bob = users.create_user(db_session, username="bob", name="Bob", password="pw")
    make_transaction(account_id=account.id, amount=1000, day=1, scope=Scope.household)
    make_transaction(
        account_id=account.id, amount=500, day=2, owner_id=alice.id, scope=Scope.private
    )

    household = {m.key: m.value for m in collect_metrics(db_session)}
    member = {m.key: m for m in collect_member_metrics(db_session)}

    assert household["net_worth"] == "10.00"
    assert member[f"owner_{alice.id}_net_worth"].value == "15.00"
    assert member[f"owner_{bob.id}_net_worth"].value == "10.00"
    assert member[f"owner_{alice.id}_net_worth"].name == "Net Worth (Alice)"
    # Per-account and per-budget sensors stay household-only.
    assert not any(k.startswith(f"owner_{alice.id}_account_") for k in member)

    # only_id is what keeps these off any in-app surface: they carry private rows,
    # so a page may show the viewer's own and nobody else's.
    mine = {m.key for m in collect_member_metrics(db_session, only_id=bob.id)}

    assert mine == {f"owner_{bob.id}_{k}" for k in ("net_worth", *_MONTH_KEYS)}
