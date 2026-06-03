"""Reconciliation tests — the two signals run independently of any bank parser.

Built on ``ParseResult`` directly so they exercise the reconciler, not a CSV
format: declared-totals checking (mBank-style) and running-balance continuity
(PKO-style), plus the no-signal case.
"""

from datetime import date

from expense_analyzer.importers.base import NormalizedTransaction, ParseResult
from expense_analyzer.importers.reconciliation import reconcile


def _tx(amount: int, balance_after: int | None = None) -> NormalizedTransaction:
    return NormalizedTransaction(date(2026, 5, 1), amount, "x", balance_after=balance_after)


def test_declared_totals_match():
    result = ParseResult(
        transactions=[_tx(1000), _tx(-300), _tx(50)],
        declared_inflow=1050,
        declared_outflow=-300,
    )
    rec = reconcile(result)
    assert rec.ok
    assert rec.label == "OK"


def test_declared_inflow_mismatch_flagged():
    result = ParseResult(transactions=[_tx(1000)], declared_inflow=9999)
    rec = reconcile(result)
    assert not rec.ok
    assert rec.label == "Mismatch"
    assert any("Inflow" in d for d in rec.details)


def test_declared_outflow_mismatch_flagged():
    result = ParseResult(transactions=[_tx(-1000)], declared_outflow=-500)
    rec = reconcile(result)
    assert not rec.ok
    assert any("Outflow" in d for d in rec.details)


def test_balance_continuity_consistent_chain():
    # Statement order newest-first (PKO): balance after each op chains cleanly.
    chain = [_tx(50, 750), _tx(-300, 700), _tx(1000, 1000)]
    rec = reconcile(ParseResult(transactions=chain))
    assert rec.ok
    assert any("consistent" in d for d in rec.details)


def test_balance_continuity_detects_gap():
    # A row was dropped between the first and last: the balance jump matches no
    # single amount, in either direction -> break.
    chain = [_tx(50, 750), _tx(1000, 1000)]
    rec = reconcile(ParseResult(transactions=chain))
    assert not rec.ok
    assert any("break" in d.lower() for d in rec.details)


def test_no_signal_is_ok_but_reports_unavailable():
    rec = reconcile(ParseResult(transactions=[_tx(100), _tx(-50)]))
    assert rec.ok
    assert rec.label == "Not available"


def test_partial_balance_chain_is_not_checked():
    # A hole in the running balance (some rows lack it) makes continuity
    # meaningless, so it is skipped rather than raising a false break.
    mixed = [_tx(50, 750), _tx(-300), _tx(1000, 1000)]
    rec = reconcile(ParseResult(transactions=mixed))
    assert rec.ok
    assert rec.label == "Not available"
