"""Pure categorization-rule matching (Phase 10, design §7.7).

No DB: these exercise :mod:`expense_analyzer.rules` directly — substring match,
case-insensitivity, the merchant/raw-description fallback, and priority ordering.
"""

from expense_analyzer.rules import RuleSpec, match_category, sort_rules


def _match(rules: list[RuleSpec], *, merchant: str | None = None, raw: str = "") -> int | None:
    return match_category(sort_rules(rules), merchant_normalized=merchant, raw_description=raw)


def test_substring_match_on_merchant() -> None:
    rules = [RuleSpec(pattern="BIEDRONKA", category_id=7)]
    assert _match(rules, merchant="BIEDRONKA 1234 WARSZAWA") == 7


def test_match_is_case_insensitive() -> None:
    rules = [RuleSpec(pattern="netflix", category_id=3)]
    assert _match(rules, merchant="NETFLIX.COM") == 3


def test_no_match_returns_none() -> None:
    rules = [RuleSpec(pattern="LIDL", category_id=1)]
    assert _match(rules, merchant="BIEDRONKA") is None


def test_falls_back_to_raw_description_when_no_merchant() -> None:
    rules = [RuleSpec(pattern="ZABKA", category_id=4)]
    assert _match(rules, merchant=None, raw="PŁATNOŚĆ KARTĄ ZABKA Z123") == 4


def test_merchant_preferred_over_raw_description() -> None:
    # The pattern is in raw but not in merchant -> no match (merchant wins when present).
    rules = [RuleSpec(pattern="LIDL", category_id=2)]
    assert _match(rules, merchant="BIEDRONKA", raw="LIDL SP Z OO") is None


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


def test_blank_pattern_is_inert() -> None:
    rules = [RuleSpec(pattern="   ", category_id=5)]
    assert _match(rules, merchant="ANYTHING") is None


def test_empty_text_returns_none() -> None:
    rules = [RuleSpec(pattern="X", category_id=1)]
    assert _match(rules, merchant=None, raw="") is None
