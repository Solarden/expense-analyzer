"""Internal-transfer detection — the pairing logic, kept pure (no DB).

A move of, say, 2000 PLN from PKO to mBank is neither an expense nor income —
it's a transfer (design §6, §7.2). Left unpaired it shows as a fake expense on
one account and a fake inflow on the other, and every spending/income number is
junk. This module finds the two legs; linking and labelling them lives in
:mod:`expense_analyzer.queries.transfers`.

Pairing rule — two transactions on **different** accounts are a candidate when:

- one is an outflow (``amount < 0``) and the other an inflow (``amount > 0``),
- their absolute amounts are equal (``outflow.amount == -inflow.amount``),
- their booked dates are within ``window_days`` of each other.

Fuzzy matching on amount and date occasionally guesses wrong, so we only
*auto-confirm* a pair when it is **mutually unique** — the outflow has exactly
one matching inflow and that inflow has exactly one matching outflow. Anything
with a choice on either side is returned as an ambiguous suggestion for a human
to confirm.
"""

from collections import defaultdict
from dataclasses import dataclass

from expense_analyzer.models import Transaction


@dataclass(frozen=True, slots=True)
class TransferPair:
    """One candidate transfer: the outflow leg, the inflow leg, and their gap."""

    outflow: Transaction
    inflow: Transaction
    date_gap_days: int


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """Outcome of :func:`find_transfer_pairs`.

    ``auto`` pairs are mutually unique and safe to link without asking; both legs
    are uncategorized (we never clobber a manual category). ``ambiguous`` pairs
    have a choice on at least one side and need manual confirmation.
    """

    auto: list[TransferPair]
    ambiguous: list[TransferPair]


def _is_candidate(outflow: Transaction, inflow: Transaction, window_days: int) -> bool:
    return (
        outflow.account_id != inflow.account_id
        and outflow.amount == -inflow.amount
        and abs((outflow.booked_date - inflow.booked_date).days) <= window_days
    )


def find_transfer_pairs(transactions: list[Transaction], *, window_days: int) -> DetectionResult:
    """Find transfer candidates among ``transactions`` (assumed already unmatched).

    Edges are bucketed by absolute amount so we only compare transactions that
    could possibly pair. A candidate edge is auto-confirmable when both of its
    endpoints have exactly one edge (mutual uniqueness) and both legs carry no
    category yet; otherwise it's an ambiguous suggestion.
    """
    outflows = [t for t in transactions if t.amount < 0]
    inflows = [t for t in transactions if t.amount > 0]

    by_amount: dict[int, list[Transaction]] = defaultdict(list)
    for inflow in inflows:
        by_amount[inflow.amount].append(inflow)

    # Candidate edges, plus a degree count per transaction id to spot ambiguity.
    edges: list[TransferPair] = []
    degree: dict[int, int] = defaultdict(int)
    for outflow in outflows:
        for inflow in by_amount.get(-outflow.amount, []):
            if not _is_candidate(outflow, inflow, window_days):
                continue
            gap = abs((outflow.booked_date - inflow.booked_date).days)
            edges.append(TransferPair(outflow=outflow, inflow=inflow, date_gap_days=gap))
            degree[outflow.id] += 1
            degree[inflow.id] += 1

    auto: list[TransferPair] = []
    ambiguous: list[TransferPair] = []
    for pair in edges:
        mutually_unique = degree[pair.outflow.id] == 1 and degree[pair.inflow.id] == 1
        uncategorized = pair.outflow.category_id is None and pair.inflow.category_id is None
        if mutually_unique and uncategorized:
            auto.append(pair)
        else:
            ambiguous.append(pair)

    return DetectionResult(auto=auto, ambiguous=ambiguous)
