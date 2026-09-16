"""Import orchestration: open a batch, upsert on fingerprint, summarize.

Flow:
1. An :class:`~expense_analyzer.models.ImportBatch` is opened.
2. The importer parses the bytes into ``NormalizedTransaction`` records.
3. A fingerprint is computed per record.
4. Upsert: known fingerprint -> skip; new -> insert, linked to the batch.
5. Return a summary (how many new, how many skipped as duplicates).

``balance_after`` is stored per record and checked by :func:`reconcile`.
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
from expense_analyzer.models import (
    Account,
    ImportBatch,
    ImportStatus,
    Scope,
    Transaction,
    TxSource,
)
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
    auto_categorized: int = 0  # new rows categorized by a rule post-import


def run_import(
    session: Session,
    *,
    account_id: int,
    importer: Importer,
    filename: str,
    data: bytes,
    owner_id: int | None = None,
) -> ImportSummary:
    """Parse ``data`` with ``importer`` and idempotently upsert into ``account_id``.

    The destination account decides what the rows become: a shared account (no
    owner) imports ``household``, feeding the home budget; a member's own account
    imports ``private``, owned by that member — not by whoever uploaded the file, or
    the owner could not see their own statement. ``owner_id`` is the uploading user
    and stays on the batch either way, because it is the batch that gates rollback.

    Commits the batch and its new transactions atomically. The batch is created
    lazily — only on the first new transaction — so re-importing the same file
    (all duplicates) adds nothing and leaves no empty batch behind.
    """
    result = importer.parse(data)
    account = session.get(Account, account_id)

    if account is None or account.owner_id is not None:
        # No account is unreachable (the handler checks first); private is simply the
        # direction a visibility default has to fail in when it cannot tell.
        scope = Scope.private
        row_owner_id = account.owner_id if account is not None else owner_id
    else:
        scope = Scope.household
        row_owner_id = owner_id

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
                owner_id=owner_id,
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
                scope=scope,
                owner_id=row_owner_id,
                fingerprint=fingerprint,
            )
        )
        new += 1

    if batch is not None:
        batch.record_count = new  # rows this batch owns (what a rollback would remove)
        session.add(batch)

    session.commit()

    # A transfer's counterpart may have arrived in an earlier batch on another
    # account, so scan *all* unmatched candidates, not just this run. Only
    # unambiguous pairs are auto-linked; the rest wait for manual confirmation.
    #
    # Both post-import steps below are viewer-scoped, so they run as the rows' owner
    # and not the uploader — otherwise they would silently skip what was just written.
    auto_linked = 0

    if new:
        try:
            auto_linked, _ = detect_and_autolink(
                session, window_days=get_settings().transfer_window_days, viewer_id=row_owner_id
            )
        except Exception:  # noqa: BLE001 — convenience step, never fail the import
            log.exception("transfer auto-link failed after import; rows are committed")
            session.rollback()  # discard the half-done detection unit of work

    # Deterministic categorization (layer 1): new rows are uncategorized, so the
    # rule matcher fills the ones a rule covers.
    auto_categorized = 0

    if new:
        try:
            auto_categorized = apply_rules(session, viewer_id=row_owner_id)
        except Exception:  # noqa: BLE001 — convenience step, never fail the import
            log.exception("rule auto-categorization failed after import; rows are committed")
            session.rollback()

    # Import stays rules-only so a large or first import never blocks on the
    # Ollama host; probabilistic categorization is an on-demand step from the
    # review queue (see queries/categorize/llm.py).

    return ImportSummary(
        batch_id=batch.id if batch else None,
        parsed=len(result.transactions),
        new=new,
        skipped=skipped,
        reconciliation=reconcile(result),
        transfers_auto_linked=auto_linked,
        auto_categorized=auto_categorized,
    )


def rollback_batch(session: Session, batch_id: int, *, viewer_id: int | None = None) -> int | None:
    """Soft-delete every transaction in a batch and mark the batch rolled back.

    Returns the number of transactions soft-deleted, or ``None`` when the batch is
    not the viewer's to roll back. A rollback removes private rows the viewer cannot
    see, so it is gated on ownership rather than on visibility.

    Missing and not-yours both return ``None`` on purpose: distinguishable answers
    would let a member enumerate other members' batch ids.

    Idempotent: already soft-deleted rows are left untouched. Nothing is hard-deleted.
    """
    batch = session.get(ImportBatch, batch_id)

    if batch is None:
        return None

    if viewer_id is not None and batch.owner_id != viewer_id:
        return None

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
