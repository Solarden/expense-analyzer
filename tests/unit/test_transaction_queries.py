"""Transaction list queries — pagination and the Phase 4 filters."""

from collections.abc import Callable
from datetime import date

from sqlmodel import Session

from expense_analyzer.models import Account, Category, CategoryKind, Scope, Transaction
from expense_analyzer.queries.money import transactions
from expense_analyzer.queries.money.transactions import TransactionFilters


def test_pagination_windows_and_counts(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    for d in range(1, 26):  # 25 rows
        make_transaction(account_id=account.id, amount=-100 * d, booked_date=date(2026, 5, d))

    p1 = transactions.list_transactions(db_session, TransactionFilters(), page=1, page_size=10)
    assert p1.total == 25
    assert p1.pages == 3
    assert len(p1.rows) == 10
    assert p1.has_prev is False
    assert p1.has_next is True
    # Newest first: day 25 leads.
    assert p1.rows[0].booked_date == date(2026, 5, 25)

    p3 = transactions.list_transactions(db_session, TransactionFilters(), page=3, page_size=10)
    assert len(p3.rows) == 5  # remainder
    assert p3.has_next is False
    assert p3.has_prev is True


def test_page_clamped_to_minimum(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(account_id=account.id, amount=-100, booked_date=date(2026, 5, 1))

    page = transactions.list_transactions(db_session, TransactionFilters(), page=0, page_size=10)

    assert page.page == 1
    assert page.pages == 1  # never zero, even on an empty match


def test_empty_match_has_one_page(db_session: Session):
    page = transactions.list_transactions(db_session, TransactionFilters(), page=1, page_size=10)

    assert page.total == 0
    assert page.pages == 1
    assert page.rows == []


def test_filter_by_month(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(account_id=account.id, amount=-100, booked_date=date(2026, 5, 15))
    make_transaction(account_id=account.id, amount=-200, booked_date=date(2026, 4, 30))
    make_transaction(account_id=account.id, amount=-300, booked_date=date(2026, 12, 31))

    page = transactions.list_transactions(
        db_session, TransactionFilters(month="2026-05"), page=1, page_size=10
    )

    assert page.total == 1
    assert page.rows[0].booked_date == date(2026, 5, 15)


def test_invalid_month_filter_is_ignored_not_fatal(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(account_id=account.id, amount=-100, booked_date=date(2026, 5, 1))

    for bad in ["abc", "2026-13", "2026-00", ""]:
        page = transactions.list_transactions(
            db_session, TransactionFilters(month=bad), page=1, page_size=10
        )
        assert page.total == 1  # filter silently skipped, no crash


def test_page_defensive_against_zero_page_size():
    from expense_analyzer.queries.money.transactions import TransactionPage

    page = TransactionPage(rows=[], total=0, page=1, page_size=0)

    assert page.pages == 1  # no ZeroDivisionError even if a 0 slips through


def test_search_escapes_like_wildcards(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(account_id=account.id, amount=-100, day=1, raw_description="50% OFF SALE")
    make_transaction(account_id=account.id, amount=-200, day=2, raw_description="NO DISCOUNT")

    # A literal '%' must match only the row that actually contains it — not act
    # as a LIKE match-all wildcard.
    page = transactions.list_transactions(
        db_session, TransactionFilters(search="%"), page=1, page_size=10
    )

    assert page.total == 1
    assert "50% OFF SALE" in page.rows[0].raw_description


def test_filter_by_month_december_boundary(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(account_id=account.id, amount=-100, booked_date=date(2026, 12, 31))
    make_transaction(account_id=account.id, amount=-200, booked_date=date(2027, 1, 1))

    page = transactions.list_transactions(
        db_session, TransactionFilters(month="2026-12"), page=1, page_size=10
    )

    assert page.total == 1  # Jan 1 of next year excluded by the half-open range


def test_filter_uncategorized(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
):
    food = make_category(name="Food", kind=CategoryKind.expense)
    make_transaction(account_id=account.id, amount=-100, day=1, category_id=food.id)
    make_transaction(account_id=account.id, amount=-200, day=2)  # no category

    page = transactions.list_transactions(
        db_session, TransactionFilters(uncategorized=True), page=1, page_size=10
    )

    assert page.total == 1
    assert page.rows[0].category_id is None


def test_filter_by_category(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
):
    food = make_category(name="Food", kind=CategoryKind.expense)
    make_transaction(account_id=account.id, amount=-100, day=1, category_id=food.id)
    make_transaction(account_id=account.id, amount=-200, day=2)

    page = transactions.list_transactions(
        db_session, TransactionFilters(category_id=food.id), page=1, page_size=10
    )

    assert page.total == 1
    assert page.rows[0].category_id == food.id


def test_filter_by_scope(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(account_id=account.id, amount=-100, day=1, scope=Scope.household)
    make_transaction(account_id=account.id, amount=-200, day=2, scope=Scope.private)

    page = transactions.list_transactions(
        db_session, TransactionFilters(scope=Scope.household), page=1, page_size=10
    )

    assert page.total == 1
    assert page.rows[0].scope == Scope.household


def test_filter_search_matches_description_and_merchant(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(account_id=account.id, amount=-100, day=1, raw_description="BIEDRONKA 123")
    make_transaction(
        account_id=account.id,
        amount=-200,
        day=2,
        raw_description="opaque",
        merchant_normalized="LIDL",
    )
    make_transaction(account_id=account.id, amount=-300, day=3, raw_description="ZABKA")

    by_raw = transactions.list_transactions(
        db_session, TransactionFilters(search="biedronka"), page=1, page_size=10
    )
    assert by_raw.total == 1  # case-insensitive

    by_merchant = transactions.list_transactions(
        db_session, TransactionFilters(search="lidl"), page=1, page_size=10
    )
    assert by_merchant.total == 1


def test_filters_combine(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    make_transaction(
        account_id=account.id, amount=-100, booked_date=date(2026, 5, 1), scope=Scope.household
    )
    make_transaction(
        account_id=account.id, amount=-200, booked_date=date(2026, 5, 2), scope=Scope.private
    )
    make_transaction(
        account_id=account.id, amount=-300, booked_date=date(2026, 4, 1), scope=Scope.household
    )

    page = transactions.list_transactions(
        db_session,
        TransactionFilters(month="2026-05", scope=Scope.household),
        page=1,
        page_size=10,
    )

    assert page.total == 1
