"""build_spec is the security boundary: it resolves valid names and drops
everything hostile/malformed without ever raising. Pure — no LLM, no DB."""

from datetime import date

from expense_analyzer.models import Account, AccountType, Category, CategoryKind
from expense_analyzer.queries.money.nl_query import QuerySpec, build_spec

_CATS = [
    Category(id=3, name="Groceries", kind=CategoryKind.expense),
    Category(id=7, name="Eating out", kind=CategoryKind.expense),
]
_ACCS = [Account(id=1, name="PKO checking", type=AccountType.bank)]


def test_resolves_valid_names_case_insensitively() -> None:
    spec = build_spec(
        {
            "category": "groceries",
            "account": "PKO CHECKING",
            "direction": "expense",
            "group_by": "month",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "min_amount": 10,
            "max_amount": 100,
        },
        _CATS,
        _ACCS,
    )

    assert spec.category_id == 3
    assert spec.account_id == 1
    assert spec.direction == "expense"
    assert spec.group_by == "month"
    assert spec.start == date(2026, 1, 1)
    assert spec.end == date(2026, 1, 31)
    assert spec.min_amount == 1000  # zł -> grosze
    assert spec.max_amount == 10000


def test_unknown_category_and_account_are_dropped() -> None:
    spec = build_spec({"category": "Nope", "account": "Ghost"}, _CATS, _ACCS)

    assert spec.category_id is None
    assert spec.account_id is None


def test_malformed_date_and_amount_are_dropped_without_raising() -> None:
    spec = build_spec(
        {
            "start_date": "2026-13-40",
            "end_date": 20260101,
            "min_amount": "lots",
            "max_amount": None,
        },
        _CATS,
        _ACCS,
    )

    assert spec.start is None
    assert spec.end is None
    assert spec.min_amount is None
    assert spec.max_amount is None


def test_bad_enums_become_none() -> None:
    spec = build_spec({"direction": "sideways", "group_by": "weekday"}, _CATS, _ACCS)

    assert spec.direction is None
    assert spec.group_by is None


def test_amount_rounds_not_truncates() -> None:
    # int(0.29 * 100) == 28 — a lost grosz. round() gets it right.
    assert build_spec({"min_amount": 0.29}, _CATS, _ACCS).min_amount == 29


def test_non_finite_amounts_are_dropped_not_raised() -> None:
    # json.loads accepts Infinity/NaN by default, so a hostile reply can carry them:
    # NaN -> ValueError, Infinity -> OverflowError. Both must drop to None (no 500).
    spec = build_spec({"min_amount": float("inf"), "max_amount": float("nan")}, _CATS, _ACCS)

    assert spec.min_amount is None
    assert spec.max_amount is None


def test_fully_garbage_dict_is_an_empty_spec() -> None:
    spec = build_spec(
        {"category": 5, "account": ["x"], "sql": "DROP TABLE txn", "min_amount": {}},
        _CATS,
        _ACCS,
    )

    assert spec == QuerySpec()


def test_empty_dict_is_safe() -> None:
    assert build_spec({}, _CATS, _ACCS) == QuerySpec()
