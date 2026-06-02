"""Bank-agnostic import contract.

A bank parser turns raw export bytes into a list of
:class:`NormalizedTransaction` — a common internal format the rest of the
pipeline understands. Each parser owns its own quirks (encoding, separator,
column layout) and is the *only* place that knows about a specific bank.
"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class NormalizedTransaction:
    """One transaction in the common, bank-agnostic format.

    Money is already in signed integer grosze (negative = expense). The
    transaction is not yet tied to an account or a batch — the pipeline adds
    those, since the same parser output is imported into the account the user
    picked at upload time.
    """

    booked_date: date
    amount: int  # grosze, signed: negative = expense, positive = inflow
    raw_description: str
    balance_after: int | None = None  # running balance from the CSV (reconciliation)
    merchant_normalized: str | None = None  # optional, parser may pre-fill


@runtime_checkable
class Importer(Protocol):
    """Parses a bank export into :class:`NormalizedTransaction` records.

    Implementations receive raw ``bytes`` (not text) because each bank uses its
    own encoding — Polish exports are frequently ``windows-1250`` — and decoding
    is the parser's responsibility.
    """

    #: Human-readable label stored on the ImportBatch, e.g. ``"PKO csv"``.
    source: str

    def parse(self, data: bytes) -> list[NormalizedTransaction]: ...
