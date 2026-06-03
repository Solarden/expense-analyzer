"""Import pipeline — the heart of the app (design §6).

The design principle is to build around the importer, not around an API: the
data source is swappable, so when a bank changes its CSV format (it will), you
replace a single :class:`~expense_analyzer.importers.base.Importer` instead of
rewriting the app.
"""

from expense_analyzer.importers.base import (
    Importer,
    ImporterError,
    NormalizedTransaction,
    ParseResult,
)
from expense_analyzer.importers.fingerprint import compute_fingerprint
from expense_analyzer.importers.merchant import normalize_merchant
from expense_analyzer.importers.pipeline import ImportSummary, rollback_batch, run_import
from expense_analyzer.importers.reconciliation import ReconciliationResult, reconcile

__all__ = [
    "Importer",
    "ImporterError",
    "ImportSummary",
    "NormalizedTransaction",
    "ParseResult",
    "ReconciliationResult",
    "compute_fingerprint",
    "normalize_merchant",
    "reconcile",
    "rollback_batch",
    "run_import",
]
