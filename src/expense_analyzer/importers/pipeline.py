"""Import orchestration: open a batch, upsert on fingerprint, summarize.

Flow (design §6):
1. An :class:`~expense_analyzer.models.ImportBatch` is opened.
2. The importer parses the bytes into ``NormalizedTransaction`` records.
3. A fingerprint is computed per record.
4. Upsert: known fingerprint -> skip; new -> insert, linked to the batch.
5. Return a summary (how many new, how many skipped as duplicates).

Reconciliation via ``balance_after`` is stored now but only validated from
Phase 2 (roadmap §11) — the column exists so no migration is needed later.
"""

import logging
from dataclasses import dataclass

from sqlmodel import Session, col, select

from expense_analyzer.clock import utc_now
from expense_analyzer.config import get_settings
from expense_analyzer.importers.base import Importer
from expense_analyzer.importers.fingerprint import compute_fingerprint
from expense_analyzer.importers.merchant import normalize_merchant
from expense_analyzer.importers.reconciliation import ReconciliationResult, reconcile
from expense_analyzer.models import ImportBatch, ImportStatus, Transaction, TxSource
from expense_analyzer.queries.categorize.rules import apply_rules
from expense_analyzer.queries.money.transfers import detect_and_autolink

log = logging.getLogger("expense_analyzer.import")


@dataclass(frozen=True, slots=True)
class ImportSummary:
    batch_id: int | None  # None when nothing new was imported (no batch created)
    parsed: int  # records the parser produced
    new: int  # inserted this run
    skipped: int  # duplicates (fingerprint already known, or repeated within the file)
    reconciliation: ReconciliationResult  # non-blocking sanity check on the parsed file
    transfers_auto_linked: int = 0  # unambiguous internal transfers paired post-import
    auto_categorized: int = 0  # new rows categorized by a rule post-import (Phase 10)


def run_import(
    session: Session,
    *,
    account_id: int,
    importer: Importer,
    filename: str,
    data: bytes,
) -> ImportSummary:
    """Parse ``data`` with ``importer`` and idempotently upsert into ``account_id``.

    Commits the batch and its new transactions atomically. The batch is created
    lazily — only on the first new transaction — so re-importing the same file
    (all duplicates) adds nothing and leaves no empty batch behind.
    """
    result = importer.parse(data)

    batch: ImportBatch | None = None
    new = 0
    skipped = 0
    # Fingerprints inserted in *this* run. Dedup is against both the DB and this
    # set, so a file that repeats an identical row imports it once (design's
    # accepted in-file-duplicate behaviour) without tripping the unique index.
    seen: set[str] = set()
    for nt in result.transactions:
        fingerprint = compute_fingerprint(account_id, nt.booked_date, nt.amount, nt.raw_description)
        if fingerprint in seen:
            skipped += 1
            continue
        already = session.exec(
            select(Transaction.id).where(Transaction.fingerprint == fingerprint)
        ).first()
        if already is not None:
            skipped += 1
            continue
        seen.add(fingerprint)

        if batch is None:
            batch = ImportBatch(
                source=importer.source,
                filename=filename,
                record_count=0,
                status=ImportStatus.active,
            )
            session.add(batch)
            session.flush()  # assign batch.id

        merchant = nt.merchant_normalized or normalize_merchant(nt.raw_description)
        session.add(
            Transaction(
                account_id=account_id,
                import_batch_id=batch.id,
                amount=nt.amount,
                balance_after=nt.balance_after,
                booked_date=nt.booked_date,
                raw_description=nt.raw_description,
                merchant_normalized=merchant,
                source=TxSource.import_csv,
                fingerprint=fingerprint,
            )
        )
        new += 1

    if batch is not None:
        batch.record_count = new  # rows this batch owns (what a rollback would remove)
        session.add(batch)

    session.commit()

    # Post-import analysis: a transfer's counterpart may have arrived in an
    # earlier batch on another account, so scan *all* unmatched candidates, not
    # just this run. Only unambiguous pairs are auto-linked; the rest wait on the
    # Transfers page for manual confirmation.
    #
    # Kept strictly non-fatal: the import has already committed above, so a
    # failure here must not turn a successful import into a 500. Auto-linking is a
    # convenience — on error we log and report 0, and the user can still pair
    # manually (or hit "Rescan") on the Transfers page.
    auto_linked = 0
    if new:
        try:
            auto_linked, _ = detect_and_autolink(
                session, window_days=get_settings().transfer_window_days
            )
        except Exception:  # noqa: BLE001 — convenience step, never fail the import
            log.exception("transfer auto-link failed after import; rows are committed")
            session.rollback()  # discard the half-done detection unit of work

    # Post-import analysis: deterministic categorization (layer 1, Phase 10). New
    # rows are uncategorized, so the rule matcher fills the ones a rule covers.
    # Same non-fatal contract as transfer auto-linking above: the import is already
    # committed, so a failure here logs and reports 0 rather than 500-ing — the user
    # can still hit "Apply rules now" or categorize by hand.
    auto_categorized = 0
    if new:
        try:
            auto_categorized = apply_rules(session)
        except Exception:  # noqa: BLE001 — convenience step, never fail the import
            log.exception("rule auto-categorization failed after import; rows are committed")
            session.rollback()

    # Probabilistic categorization (the Ollama host, or the local classifier as
    # fallback) is no longer run at import — it's an on-demand step from the review
    # queue ("classify now"). Import stays rules-only, so a big or first import
    # isn't blocked waiting on the Ollama host. See queries/categorize/llm.py.

    return ImportSummary(
        batch_id=batch.id if batch else None,
        parsed=len(result.transactions),
        new=new,
        skipped=skipped,
        reconciliation=reconcile(result),
        transfers_auto_linked=auto_linked,
        auto_categorized=auto_categorized,
    )


def rollback_batch(session: Session, batch_id: int) -> int:
    """Soft-delete every transaction in a batch and mark the batch rolled back.

    Returns the number of transactions soft-deleted. Idempotent: already
    soft-deleted rows are left untouched. Nothing is hard-deleted (design §6).
    """
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise ValueError(f"no import batch with id {batch_id}")

    now = utc_now()
    rows = session.exec(
        select(Transaction).where(
            Transaction.import_batch_id == batch_id,
            col(Transaction.deleted_at).is_(None),
        )
    ).all()
    for tx in rows:
        tx.deleted_at = now
        session.add(tx)

    batch.status = ImportStatus.rolled_back
    session.add(batch)
    session.commit()

    return len(rows)
