"""Categorization rules — layer 1, the deterministic matcher (design §7.7).

Pure logic, zero DB (mirrors :mod:`expense_analyzer.transfers` /
:mod:`expense_analyzer.subscriptions`). A rule is a case-insensitive **substring**
``pattern``; it matches a transaction when the pattern appears in its
``merchant_normalized`` — or, when no merchant was extracted, its
``raw_description``. Spending is repetitive, so a handful of substring rules covers
most of it without any ML (the goal of layer 1).

Rules are ranked by ``priority`` (higher first), ties broken by ``order`` (the
older rule first); the first match in that order wins. The query layer builds the
ordered :class:`RuleSpec` list from the stored rows and writes the result back.
"""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """A rule reduced to what matching needs: the pattern, the category it assigns,
    and its ordering key. Decoupled from the ORM row so the matcher stays pure and
    trivially testable without a session."""

    pattern: str
    category_id: int
    priority: int = 0
    order: int = 0  # tie-breaker within a priority (the stored row id; older first)


def sort_rules(rules: Sequence[RuleSpec]) -> list[RuleSpec]:
    """Rules in evaluation order: highest ``priority`` first, ties by ``order``
    ascending (the older rule first)."""
    return sorted(rules, key=lambda r: (-r.priority, r.order))


def match_category(
    rules: Sequence[RuleSpec], *, merchant_normalized: str | None, raw_description: str
) -> int | None:
    """The category id assigned by the first matching rule, or ``None``.

    ``rules`` must already be in evaluation order (see :func:`sort_rules`). The
    pattern is matched case-insensitively as a substring, against
    ``merchant_normalized`` when present, otherwise ``raw_description``. A blank
    pattern never matches — it would match everything, so a blank rule is inert
    rather than a catch-all.
    """
    text = (merchant_normalized or raw_description or "").casefold()
    if not text:
        return None

    for rule in rules:
        needle = rule.pattern.strip().casefold()
        if needle and needle in text:
            return rule.category_id

    return None
