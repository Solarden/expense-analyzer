"""Pure categorization-rule matching.

No DB: these exercise :mod:`expense_analyzer.rules` directly — substring match,
case-insensitivity, the merchant/raw-description fallback, and priority ordering.
"""

import pytest

from expense_analyzer.rules import RuleSpec, match_category, sort_rules


def _match(rules: list[RuleSpec], *, merchant: str | None = None, raw: str = "") -> int | None:
    return match_category(sort_rules(rules), merchant_normalized=merchant, raw_description=raw)


@pytest.mark.parametrize(
    ("pattern", "category_id", "merchant", "raw", "expected"),
    [
        pytest.param("BIEDRONKA", 7, "BIEDRONKA 1234 WARSZAWA", "", 7, id="substring-on-merchant"),
        pytest.param("netflix", 3, "NETFLIX.COM", "", 3, id="case-insensitive"),
        pytest.param("LIDL", 1, "BIEDRONKA", "", None, id="no-match"),
        pytest.param("ZABKA", 4, None, "PŁATNOŚĆ KARTĄ ZABKA Z123", 4, id="falls-back-to-raw"),
        # Pattern is in raw but not in merchant: merchant wins whenever it is present.
        pytest.param("LIDL", 2, "BIEDRONKA", "LIDL SP Z OO", None, id="merchant-beats-raw"),
        pytest.param("   ", 5, "ANYTHING", "", None, id="blank-pattern-inert"),
        pytest.param("X", 1, None, "", None, id="empty-text"),
    ],
)
def test_match_category(
    pattern: str, category_id: int, merchant: str | None, raw: str, expected: int | None
) -> None:
    rules = [RuleSpec(pattern=pattern, category_id=category_id)]

    assert _match(rules, merchant=merchant, raw=raw) == expected


def test_higher_priority_wins() -> None:
    rules = [
        RuleSpec(pattern="MARKET", category_id=1, priority=0),
        RuleSpec(pattern="BIEDRONKA", category_id=2, priority=10),
    ]

    # Both patterns match "BIEDRONKA MARKET"; the higher-priority rule decides.
    assert _match(rules, merchant="BIEDRONKA MARKET") == 2


def test_ties_broken_by_order_older_first() -> None:
    rules = [
        RuleSpec(pattern="SHOP", category_id=1, priority=5, order=2),
        RuleSpec(pattern="SHOP", category_id=9, priority=5, order=1),
    ]

    # Same priority -> the lower `order` (older row) wins.
    assert _match(rules, merchant="CORNER SHOP") == 9
