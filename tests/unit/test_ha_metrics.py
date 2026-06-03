"""Home Assistant metric collection (Phase 7).

Money is converted to a display decimal string at this edge; transfers stay
excluded from the spending/income figures (consistent with queries/stats).
"""

from collections.abc import Callable

from sqlmodel import Session

from expense_analyzer.clock import local_today
from expense_analyzer.ha.metrics import collect_metrics
from expense_analyzer.models import Account, AccountType, Transaction


def test_empty_database_still_yields_headline_metrics(db_session: Session) -> None:
    metrics = {m.key: m for m in collect_metrics(db_session)}

    # The four headline sensors exist even with no data, all reading zero.
    assert metrics["net_worth"].value == "0.00"
    assert metrics["month_spending"].value == "0.00"
    assert metrics["month_income"].value == "0.00"
    assert metrics["month_net"].value == "0.00"


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
