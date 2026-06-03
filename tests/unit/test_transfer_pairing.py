"""Unit tests for the pure transfer-pairing logic (no DB)."""

from datetime import date

from expense_analyzer.models import Transaction
from expense_analyzer.transfers import find_transfer_pairs


def _tx(
    tx_id: int,
    account_id: int,
    amount: int,
    day: int,
    *,
    category_id: int | None = None,
) -> Transaction:
    return Transaction(
        id=tx_id,
        account_id=account_id,
        import_batch_id=1,
        amount=amount,
        booked_date=date(2026, 5, day),
        raw_description=f"tx{tx_id}",
        fingerprint=f"fp{tx_id}",
        category_id=category_id,
    )


def test_clean_opposite_pair_auto_links():
    out = _tx(1, account_id=1, amount=-200000, day=1)
    inn = _tx(2, account_id=2, amount=200000, day=2)

    result = find_transfer_pairs([out, inn], window_days=3)

    assert len(result.auto) == 1
    assert not result.ambiguous
    pair = result.auto[0]
    assert pair.outflow.id == 1 and pair.inflow.id == 2
    assert pair.date_gap_days == 1


def test_same_sign_does_not_pair():
    # Two outflows of equal magnitude are not a transfer.
    a = _tx(1, account_id=1, amount=-200000, day=1)
    b = _tx(2, account_id=2, amount=-200000, day=1)

    result = find_transfer_pairs([a, b], window_days=3)

    assert not result.auto and not result.ambiguous


def test_same_account_does_not_pair():
    out = _tx(1, account_id=1, amount=-200000, day=1)
    inn = _tx(2, account_id=1, amount=200000, day=1)

    result = find_transfer_pairs([out, inn], window_days=3)

    assert not result.auto and not result.ambiguous


def test_unequal_amount_does_not_pair():
    out = _tx(1, account_id=1, amount=-200000, day=1)
    inn = _tx(2, account_id=2, amount=199999, day=1)

    result = find_transfer_pairs([out, inn], window_days=3)

    assert not result.auto and not result.ambiguous


def test_window_boundary_inclusive_then_excluded():
    out = _tx(1, account_id=1, amount=-200000, day=1)
    inn_edge = _tx(2, account_id=2, amount=200000, day=4)  # gap 3 — within
    inn_far = _tx(3, account_id=2, amount=200000, day=5)  # gap 4 — outside

    within = find_transfer_pairs([out, inn_edge], window_days=3)
    assert len(within.auto) == 1

    outside = find_transfer_pairs([out, inn_far], window_days=3)
    assert not outside.auto and not outside.ambiguous


def test_two_inflow_choices_make_it_ambiguous():
    # One outflow, two equally-valid inflows -> not auto, both edges suggested.
    out = _tx(1, account_id=1, amount=-200000, day=2)
    inn_a = _tx(2, account_id=2, amount=200000, day=1)
    inn_b = _tx(3, account_id=2, amount=200000, day=3)

    result = find_transfer_pairs([out, inn_a, inn_b], window_days=3)

    assert not result.auto
    assert len(result.ambiguous) == 2


def test_auto_skips_already_categorized_leg():
    # A mutually-unique pair where one leg is already categorized is left as a
    # suggestion — auto-link never clobbers a manual category.
    out = _tx(1, account_id=1, amount=-200000, day=1, category_id=7)
    inn = _tx(2, account_id=2, amount=200000, day=2)

    result = find_transfer_pairs([out, inn], window_days=3)

    assert not result.auto
    assert len(result.ambiguous) == 1
