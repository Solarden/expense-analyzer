"""Natural-language query execution (PR 4), against the DB with a stubbed client.

The Ollama client is faked (no network): ``parse_query`` returns a canned raw dict
or raises :class:`OllamaError`. Totals/breakdowns/filters are checked through
:func:`answer`; the /dashboard/ask page render is checked through the auth client.
"""

from collections.abc import Callable
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import Session

from expense_analyzer.config import Settings
from expense_analyzer.models import Account, Category, CategoryKind, Loan, Transaction
from expense_analyzer.ollama import OllamaError
from expense_analyzer.queries.money.nl_query import answer

_LLM = Settings(llm_enabled=True, llm_base_url="http://ollama:11434")


class _FakeClient:
    """Stands in for OllamaClient: returns a canned raw dict, or raises to simulate a
    down Ollama host. Records call count so a test can assert the LLM path was (not) taken."""

    def __init__(self, raw: dict | None = None, *, error: bool = False) -> None:
        self._raw = raw
        self._error = error
        self.calls = 0

    def parse_query(self, question: str, **_kw: object) -> dict:
        self.calls += 1
        if self._error:
            raise OllamaError("Ollama host down")
        assert self._raw is not None
        return self._raw


def test_total_and_category_breakdown(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    make_transaction(account_id=account.id, amount=-5000, category_id=food.id)
    make_transaction(account_id=account.id, amount=-3000, category_id=food.id)
    make_transaction(account_id=account.id, amount=-2000, category_id=fun.id)
    raw = {"group_by": "category", "direction": "expense", "interpretation": "spending by category"}

    res = answer(db_session, "spending by category", settings=_LLM, client=_FakeClient(raw))

    assert res.ok
    assert res.total == 10000
    assert res.matched == 3
    assert res.group_by == "category"
    assert [(c.name, c.total) for c in res.breakdown] == [("Food", 8000), ("Fun", 2000)]
    assert res.interpretation == "spending by category"


def test_category_filter_narrows_the_total(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    make_transaction(account_id=account.id, amount=-5000, category_id=food.id)
    make_transaction(account_id=account.id, amount=-3000, category_id=food.id)
    make_transaction(account_id=account.id, amount=-2000, category_id=fun.id)
    raw = {"category": "Food", "direction": "expense", "interpretation": "food only"}

    res = answer(db_session, "food", settings=_LLM, client=_FakeClient(raw))

    assert res.total == 8000
    assert res.matched == 2


def test_month_breakdown(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
) -> None:
    make_transaction(account_id=account.id, amount=-5000, booked_date=date(2026, 4, 10))
    make_transaction(account_id=account.id, amount=-3000, booked_date=date(2026, 5, 10))
    make_transaction(account_id=account.id, amount=10000, booked_date=date(2026, 5, 20))  # income
    raw = {"group_by": "month", "interpretation": "by month"}

    res = answer(db_session, "by month", settings=_LLM, client=_FakeClient(raw))

    assert res.total == 18000  # no direction filter -> all magnitudes
    by_month = {m.month: (m.spending, m.income) for m in res.breakdown}
    assert by_month == {"2026-04": (5000, 0), "2026-05": (3000, 10000)}


def test_amount_filter_bites(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
) -> None:
    make_transaction(account_id=account.id, amount=-5000)  # 50 zł
    make_transaction(account_id=account.id, amount=-15000)  # 150 zł
    raw = {"min_amount": 100, "direction": "expense", "interpretation": "over 100 zł"}

    res = answer(db_session, "over 100", settings=_LLM, client=_FakeClient(raw))

    assert res.matched == 1
    assert res.total == 15000


def test_date_range_bites(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
) -> None:
    make_transaction(account_id=account.id, amount=-5000, booked_date=date(2026, 5, 10))
    make_transaction(account_id=account.id, amount=-3000, booked_date=date(2026, 6, 10))
    raw = {"start_date": "2026-05-01", "end_date": "2026-05-31", "interpretation": "may"}

    res = answer(db_session, "may", settings=_LLM, client=_FakeClient(raw))

    assert res.matched == 1
    assert res.total == 5000


def test_transfers_and_loans_are_excluded_and_broad_query_runs(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
    make_loan: Callable[..., Loan],
) -> None:
    make_transaction(account_id=account.id, amount=-5000)  # normal spendable expense
    transfer = make_category(name="Transfer", kind=CategoryKind.transfer)
    make_transaction(account_id=account.id, amount=-9999, category_id=transfer.id)  # excluded
    loan = make_loan(account_id=account.id)
    make_transaction(account_id=account.id, amount=-8888, loan_id=loan.id)  # excluded
    raw = {"interpretation": "everything"}  # no filters -> a valid broad total

    res = answer(db_session, "total spending", settings=_LLM, client=_FakeClient(raw))

    assert res.ok
    assert res.matched == 1
    assert res.total == 5000


def test_llm_disabled_returns_disabled_and_never_calls_client(db_session: Session) -> None:
    client = _FakeClient(error=True)  # would raise if consulted

    res = answer(db_session, "anything", settings=Settings(llm_enabled=False), client=client)

    assert not res.ok
    assert client.calls == 0
    assert "disabled" in res.interpretation.lower()


def test_ollama_error_is_a_friendly_result_not_a_crash(db_session: Session) -> None:
    client = _FakeClient(error=True)

    res = answer(db_session, "boom", settings=_LLM, client=client)

    assert not res.ok
    assert client.calls == 1
    assert res.total == 0
    assert res.rows == []


def test_missing_interpretation_is_uninterpretable(db_session: Session) -> None:
    client = _FakeClient({"category": "Food"})  # no interpretation field

    res = answer(db_session, "x", settings=_LLM, client=client)

    assert not res.ok


def test_ask_page_renders_without_q(auth_client: TestClient) -> None:
    resp = auth_client.get("/dashboard/ask")

    assert resp.status_code == 200
    assert "Ask" in resp.text


def test_ask_page_renders_with_q(auth_client: TestClient) -> None:
    # LLM is disabled in the test env, so the page answers with the disabled hint —
    # the point is it renders (no 500), with or without a question.
    resp = auth_client.get("/dashboard/ask", params={"q": "how much on food last month"})

    assert resp.status_code == 200
