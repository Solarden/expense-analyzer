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

from dataclasses import dataclass

from sqlmodel import Session, col, select

from expense_analyzer.clock import utc_now
from expense_analyzer.importers.base import Importer
from expense_analyzer.importers.fingerprint import compute_fingerprint
from expense_analyzer.models import ImportBatch, ImportStatus, Transaction, TxSource


@dataclass(frozen=True, slots=True)
class ImportSummary:
    batch_id: int | None  # None when nothing new was imported (no batch created)
    parsed: int  # records the parser produced
    new: int  # inserted this run
    skipped: int  # duplicates (fingerprint already known)


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
    parsed = importer.parse(data)

    batch: ImportBatch | None = None
    new = 0
    skipped = 0
    for nt in parsed:
        fingerprint = compute_fingerprint(account_id, nt.booked_date, nt.amount, nt.raw_description)
        already = session.exec(
            select(Transaction.id).where(Transaction.fingerprint == fingerprint)
        ).first()
        if already is not None:
            skipped += 1
            continue

        if batch is None:
            batch = ImportBatch(
                source=importer.source,
                filename=filename,
                record_count=0,
                status=ImportStatus.active,
            )
            session.add(batch)
            session.flush()  # assign batch.id

        session.add(
            Transaction(
                account_id=account_id,
                import_batch_id=batch.id,
                amount=nt.amount,
                balance_after=nt.balance_after,
                booked_date=nt.booked_date,
                raw_description=nt.raw_description,
                merchant_normalized=nt.merchant_normalized,
                source=TxSource.import_csv,
                fingerprint=fingerprint,
            )
        )
        new += 1

    if batch is not None:
        batch.record_count = new  # rows this batch owns (what a rollback would remove)
        session.add(batch)

    session.commit()

    return ImportSummary(
        batch_id=batch.id if batch else None,
        parsed=len(parsed),
        new=new,
        skipped=skipped,
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
