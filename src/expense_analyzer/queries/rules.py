"""Rule queries — the DB side of categorization layer 1 (design §7.7).

CRUD over stored rules plus :func:`apply_rules`, which runs the pure matcher
(:mod:`expense_analyzer.rules`) over candidate transactions and writes the matched
category back. Applied both at import time (the new rows, via the import pipeline)
and on demand from the Rules page ("Apply rules now").

Overwrite policy (owner's call): a rule (re)categorizes a transaction that is
**uncategorized or was itself set by a rule** (``source = rule``) — so editing a
rule and re-applying re-categorizes rule-driven rows — but it **never** overwrites
a manual categorization (``source = manual``) or a classifier's. A row a rule no
longer matches keeps its last category (rules only ever set, never clear); delete
it manually if a removed rule left a stale tag.
"""

from sqlmodel import Session, col, select

from expense_analyzer.models import Rule, Transaction, TxSource
from expense_analyzer.rules import RuleSpec, match_category, sort_rules


def list_rules(session: Session) -> list[Rule]:
    """All rules in evaluation order: highest priority first, ties by id (older first)."""
    return list(session.exec(select(Rule).order_by(col(Rule.priority).desc(), col(Rule.id))).all())


def create_rule(session: Session, *, pattern: str, category_id: int, priority: int = 0) -> Rule:
    rule = Rule(pattern=pattern.strip(), category_id=category_id, priority=priority)
    session.add(rule)
    session.commit()
    session.refresh(rule)

    return rule


def delete_rule(session: Session, rule_id: int) -> bool:
    """Delete a rule. Returns False if it doesn't exist.

    Hard delete — a rule is config, not a financial record (like budgets, unlike
    transactions). Existing categorizations it made are left in place.
    """
    rule = session.get(Rule, rule_id)
    if rule is None:
        return False

    session.delete(rule)
    session.commit()

    return True


def _rule_specs(rules: list[Rule]) -> list[RuleSpec]:
    """Stored rows -> ordered, DB-free specs for the pure matcher (id is the
    tie-breaker, so an unsaved row sorts last via ``order=0``)."""
    return sort_rules(
        [
            RuleSpec(
                pattern=r.pattern,
                category_id=r.category_id,
                priority=r.priority,
                order=r.id or 0,
            )
            for r in rules
        ]
    )


def apply_rules(session: Session) -> int:
    """Categorize eligible transactions with the current rules. Returns how many
    rows changed category.

    Eligible = not deleted, and either:

    - previously set by a rule (``source = rule``) — so editing a rule and
      re-applying re-categorizes rule-driven rows; or
    - imported-and-untouched (``source = import_csv`` *and* uncategorized).

    A human's verdict (``source = manual``) is **never** touched — including a row
    a human deliberately *cleared* to uncategorized (``category_id IS NULL`` but
    ``source = manual``), which is why the filter keys on source, not just on a
    null category. A classifier's row (``source = classifier``, Phase 11) is left
    alone too. Auto-linked transfer legs (categorized ``Transfer`` with
    ``source = import_csv``) have a category set, so they're not eligible. An
    *unconfirmed ambiguous* transfer leg is still uncategorized, so a rule matching
    its description could tag it as spending — but confirming the transfer overwrites
    that with the ``Transfer`` category, so it self-corrects (and a leg's merchant is
    a counterparty account, which rarely matches a spend rule anyway).

    Each match sets ``category_id``, ``source = rule`` and ``confidence = 1.0`` (a
    substring rule is deterministic). A row already in its matched category is left
    as-is and not counted.
    """
    rules = _rule_specs(list_rules(session))
    if not rules:
        return 0

    imported_and_uncategorized = col(Transaction.category_id).is_(None) & (
        Transaction.source == TxSource.import_csv
    )
    candidates = session.exec(
        select(Transaction).where(
            col(Transaction.deleted_at).is_(None),
            (Transaction.source == TxSource.rule) | imported_and_uncategorized,
        )
    ).all()

    changed = 0
    for tx in candidates:
        category_id = match_category(
            rules,
            merchant_normalized=tx.merchant_normalized,
            raw_description=tx.raw_description,
        )
        if category_id is None or category_id == tx.category_id:
            continue
        tx.category_id = category_id
        tx.source = TxSource.rule
        tx.confidence = 1.0
        session.add(tx)
        changed += 1

    session.commit()

    return changed
