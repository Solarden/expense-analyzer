"""Stats queries — the figures behind the overview charts.

The load-bearing rule is that **transfers never count** as spending or income
(design §6): a row is dropped if it has a ``transfer_group_id`` or sits in a
``kind=transfer`` category. Everything else is straightforward sign-based
bucketing in integer minor units.
"""

from collections.abc import Callable
from datetime import date

from sqlmodel import Session

from expense_analyzer.models import Account, Category, CategoryKind, Transaction
from expense_analyzer.queries import stats


def _names(*cats: Category) -> dict[int, str]:
    return {c.id: c.name for c in cats if c.id is not None}


def test_month_summary_sign_split_and_net(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(account_id=account.id, amount=-5000, booked_date=date(2026, 5, 3))
    make_transaction(account_id=account.id, amount=-2500, booked_date=date(2026, 5, 9))
    make_transaction(account_id=account.id, amount=10000, booked_date=date(2026, 5, 1))

    summary = stats.month_summary(stats.spendable_transactions(db_session), "2026-05", {})

    assert summary.spending == 7500  # magnitudes summed
    assert summary.income == 10000
    assert summary.net == 2500


def test_month_summary_filters_by_month(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(account_id=account.id, amount=-5000, booked_date=date(2026, 5, 3))
    make_transaction(account_id=account.id, amount=-9999, booked_date=date(2026, 4, 30))

    summary = stats.month_summary(stats.spendable_transactions(db_session), "2026-05", {})

    assert summary.spending == 5000  # April row excluded


def test_month_summary_excludes_grouped_transfer(
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
):
    a = make_account(name="PKO")
    b = make_account(name="mBank")
    # A confirmed transfer pair: equal-and-opposite, linked by a shared group id.
    make_transaction(
        account_id=a.id, amount=-200000, booked_date=date(2026, 5, 5), transfer_group_id="g1"
    )
    make_transaction(
        account_id=b.id, amount=200000, booked_date=date(2026, 5, 5), transfer_group_id="g1"
    )
    make_transaction(account_id=a.id, amount=-3000, booked_date=date(2026, 5, 6))

    summary = stats.month_summary(stats.spendable_transactions(db_session), "2026-05", {})

    assert summary.spending == 3000  # only the real expense
    assert summary.income == 0


def test_month_summary_excludes_transfer_category_without_group(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
):
    transfer_cat = make_category(name="Transfer", kind=CategoryKind.transfer)
    make_transaction(
        account_id=account.id,
        amount=-50000,
        booked_date=date(2026, 5, 5),
        category_id=transfer_cat.id,
    )
    make_transaction(account_id=account.id, amount=-1000, booked_date=date(2026, 5, 6))

    summary = stats.month_summary(stats.spendable_transactions(db_session), "2026-05", {})

    assert summary.spending == 1000  # transfer-categorized row dropped


def test_month_summary_excludes_soft_deleted(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    from expense_analyzer.clock import utc_now

    make_transaction(account_id=account.id, amount=-7000, booked_date=date(2026, 5, 2))
    make_transaction(
        account_id=account.id, amount=-4000, booked_date=date(2026, 5, 2), deleted_at=utc_now()
    )

    summary = stats.month_summary(stats.spendable_transactions(db_session), "2026-05", {})

    assert summary.spending == 7000


def test_month_summary_category_breakdown_sorted_with_uncategorized(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
):
    food = make_category(name="Food", kind=CategoryKind.expense)
    fun = make_category(name="Fun", kind=CategoryKind.expense)
    make_transaction(
        account_id=account.id, amount=-3000, booked_date=date(2026, 5, 1), category_id=food.id
    )
    make_transaction(
        account_id=account.id, amount=-8000, booked_date=date(2026, 5, 2), category_id=fun.id
    )
    make_transaction(account_id=account.id, amount=-1000, booked_date=date(2026, 5, 3))  # no cat

    summary = stats.month_summary(
        stats.spendable_transactions(db_session), "2026-05", _names(food, fun)
    )

    assert [(c.name, c.total) for c in summary.by_category] == [
        ("Fun", 8000),  # largest first
        ("Food", 3000),
        (stats.UNCATEGORIZED_LABEL, 1000),
    ]


def test_spending_trend_buckets_by_month_last_n(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(account_id=account.id, amount=-1000, booked_date=date(2026, 3, 1))
    make_transaction(account_id=account.id, amount=-2000, booked_date=date(2026, 4, 1))
    make_transaction(account_id=account.id, amount=5000, booked_date=date(2026, 4, 2))
    make_transaction(account_id=account.id, amount=-3000, booked_date=date(2026, 5, 1))

    trend = stats.spending_trend(stats.spendable_transactions(db_session), months=2)

    # Oldest-first, only the last 2 active months (March dropped).
    assert [(m.month, m.spending, m.income) for m in trend] == [
        ("2026-04", 2000, 5000),
        ("2026-05", 3000, 0),
    ]


def test_available_months_distinct_desc_includes_transfers(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(account_id=account.id, amount=-1000, booked_date=date(2026, 5, 1))
    make_transaction(account_id=account.id, amount=-1000, booked_date=date(2026, 5, 9))
    make_transaction(
        account_id=account.id, amount=-1000, booked_date=date(2026, 3, 1), transfer_group_id="g1"
    )

    assert stats.available_months(db_session) == ["2026-05", "2026-03"]


def test_default_month_prefers_request_then_newest_then_current():
    months = ["2026-05", "2026-03"]
    # An explicit request always wins.
    assert stats.default_month(months, "2026-01") == "2026-01"
    # No request -> newest month with data.
    assert stats.default_month(months, None) == "2026-05"
    # No request and no data -> a current local YYYY-MM (not a crash / blank).
    fallback = stats.default_month([], None)
    assert len(fallback) == 7 and fallback[4] == "-"
