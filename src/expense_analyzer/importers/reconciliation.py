"""Reconciliation (design §6): free error detection on a parsed export.

The job is to catch a dropped or double-counted row *at import time* rather than
discovering weeks later that the dashboard stopped matching the bank. It is
non-blocking: a mismatch is surfaced as a warning on the import summary, never an
:class:`~expense_analyzer.importers.base.ImporterError` — the data still imports,
but the user is told to look.

Banks expose different signals, so we run whichever is present:

- **Declared totals** (mBank): the export states period inflow/outflow sums; we
  compare them against the sum of the rows we parsed. Order-independent, exact.
- **Balance continuity** (PKO): every row carries the running balance after it,
  so between two adjacent statement rows the balance must move by exactly one
  amount. A break means a row went missing or got counted twice.
"""

from dataclasses import dataclass, field

from expense_analyzer.importers.base import NormalizedTransaction, ParseResult
from expense_analyzer.money import format_pln


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    ok: bool
    label: str  # short status for the dashboard, e.g. "Totals match"
    details: list[str] = field(default_factory=list)  # what was checked / what diverged


def _check_declared_totals(result: ParseResult, details: list[str]) -> bool:
    """Compare parsed inflow/outflow sums against the bank's declared totals."""
    ok = True
    inflow = sum(t.amount for t in result.transactions if t.amount > 0)
    outflow = sum(t.amount for t in result.transactions if t.amount < 0)

    if result.declared_inflow is not None:
        if inflow == result.declared_inflow:
            details.append(f"Inflow {format_pln(inflow)} matches declared total.")
        else:
            ok = False
            details.append(
                f"Inflow {format_pln(inflow)} ≠ declared {format_pln(result.declared_inflow)}."
            )
    if result.declared_outflow is not None:
        if outflow == result.declared_outflow:
            details.append(f"Outflow {format_pln(outflow)} matches declared total.")
        else:
            ok = False
            details.append(
                f"Outflow {format_pln(outflow)} ≠ declared {format_pln(result.declared_outflow)}."
            )

    return ok


def _check_balance_continuity(
    transactions: list[NormalizedTransaction], details: list[str]
) -> bool:
    """Verify the running balance moves by exactly one amount between adjacent rows.

    Rows are in statement (file) order, which may be newest-first (PKO) or
    oldest-first; we accept either, since a genuine gap breaks both relations.
    """
    chain = [t for t in transactions if t.balance_after is not None]
    if len(chain) < 2:
        return True

    breaks = 0
    for prev, cur in zip(chain, chain[1:], strict=False):
        # newest-first: prev is the newer row, so prev.balance = cur.balance + prev.amount
        newest_first = prev.balance_after - prev.amount == cur.balance_after
        # oldest-first: cur is the newer row, so cur.balance = prev.balance + cur.amount
        oldest_first = prev.balance_after + cur.amount == cur.balance_after
        if not (newest_first or oldest_first):
            breaks += 1

    if breaks:
        details.append(
            f"Running balance breaks at {breaks} of {len(chain) - 1} steps "
            "— a row may be missing or double-counted."
        )
        return False

    details.append(f"Running balance consistent across {len(chain)} rows.")
    return True


def reconcile(result: ParseResult) -> ReconciliationResult:
    """Run every reconciliation check the export supports."""
    details: list[str] = []
    checked = False
    ok = True

    if result.declared_inflow is not None or result.declared_outflow is not None:
        checked = True
        ok = _check_declared_totals(result, details) and ok

    # Only when *every* row carries a balance: a hole in the chain would make
    # two non-adjacent rows look adjacent and raise a false break.
    if result.transactions and all(t.balance_after is not None for t in result.transactions):
        checked = True
        ok = _check_balance_continuity(result.transactions, details) and ok

    if not checked:
        return ReconciliationResult(
            ok=True,
            label="Not available",
            details=["This export carries no running balance or declared totals to check."],
        )

    return ReconciliationResult(ok=ok, label="OK" if ok else "Mismatch", details=details)
