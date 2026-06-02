"""Tests for the bank-agnostic import core: idempotent upsert and rollback.

Uses a fake in-memory importer so these cover the pipeline itself, independent
of any specific bank's CSV format.
"""

from datetime import date

import pytest
from sqlmodel import Session, select

from expense_analyzer.importers import (
    NormalizedTransaction,
    compute_fingerprint,
    rollback_batch,
    run_import,
)
from expense_analyzer.models import (
    Account,
    AccountType,
    ImportBatch,
    ImportStatus,
    Transaction,
    TxSource,
)


class FakeImporter:
    """An Importer that just returns whatever records it was handed."""

    source = "fake csv"

    def __init__(self, records: list[NormalizedTransaction]) -> None:
        self._records = records

    def parse(self, data: bytes) -> list[NormalizedTransaction]:
        return self._records


@pytest.fixture
def account(db_session: Session) -> Account:
    acc = Account(name="PKO checking", type=AccountType.bank)
    db_session.add(acc)
    db_session.commit()
    db_session.refresh(acc)
    return acc


def _records() -> list[NormalizedTransaction]:
    return [
        NormalizedTransaction(date(2026, 5, 1), -12345, "Biedronka", balance_after=500000),
        NormalizedTransaction(date(2026, 5, 2), 1000000, "Wyplata", balance_after=1500000),
        NormalizedTransaction(date(2026, 5, 3), -4999, "Spotify"),
    ]


def test_import_inserts_new_transactions(db_session: Session, account: Account):
    summary = run_import(
        db_session,
        account_id=account.id,
        importer=FakeImporter(_records()),
        filename="may.csv",
        data=b"",
    )

    assert summary.parsed == 3
    assert summary.new == 3
    assert summary.skipped == 0

    rows = db_session.exec(select(Transaction)).all()
    assert len(rows) == 3
    assert all(tx.source == TxSource.import_csv for tx in rows)
    assert all(tx.import_batch_id == summary.batch_id for tx in rows)

    batch = db_session.get(ImportBatch, summary.batch_id)
    assert batch.record_count == 3
    assert batch.status == ImportStatus.active


def test_reimport_same_file_is_idempotent(db_session: Session, account: Account):
    importer = FakeImporter(_records())
    first = run_import(
        db_session, account_id=account.id, importer=importer, filename="may.csv", data=b""
    )
    second = run_import(
        db_session, account_id=account.id, importer=importer, filename="may.csv", data=b""
    )

    assert first.new == 3
    assert second.new == 0
    assert second.skipped == 3
    # Re-import creates no duplicate rows and — since nothing was new — no batch.
    assert len(db_session.exec(select(Transaction)).all()) == 3
    assert second.batch_id is None
    assert len(db_session.exec(select(ImportBatch)).all()) == 1


def test_overlapping_import_inserts_only_the_new_rows(db_session: Session, account: Account):
    run_import(
        db_session,
        account_id=account.id,
        importer=FakeImporter(_records()[:2]),
        filename="d1.csv",
        data=b"",
    )
    # A later export overlaps the first two rows and adds one more.
    summary = run_import(
        db_session,
        account_id=account.id,
        importer=FakeImporter(_records()),
        filename="d2.csv",
        data=b"",
    )
    assert summary.new == 1
    assert summary.skipped == 2
    assert len(db_session.exec(select(Transaction)).all()) == 3


def test_fingerprint_is_account_scoped(db_session: Session, account: Account):
    other = Account(name="mBank", type=AccountType.bank)
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    rec = _records()[:1]
    run_import(
        db_session, account_id=account.id, importer=FakeImporter(rec), filename="a.csv", data=b""
    )
    summary = run_import(
        db_session, account_id=other.id, importer=FakeImporter(rec), filename="b.csv", data=b""
    )
    # Same row, different account -> not a duplicate.
    assert summary.new == 1
    assert len(db_session.exec(select(Transaction)).all()) == 2


def test_rollback_soft_deletes_batch(db_session: Session, account: Account):
    summary = run_import(
        db_session,
        account_id=account.id,
        importer=FakeImporter(_records()),
        filename="may.csv",
        data=b"",
    )

    deleted = rollback_batch(db_session, summary.batch_id)
    assert deleted == 3

    batch = db_session.get(ImportBatch, summary.batch_id)
    assert batch.status == ImportStatus.rolled_back

    rows = db_session.exec(select(Transaction)).all()
    assert len(rows) == 3  # soft delete: rows still present
    assert all(tx.deleted_at is not None for tx in rows)

    # Idempotent: a second rollback removes nothing more.
    assert rollback_batch(db_session, summary.batch_id) == 0


def test_compute_fingerprint_is_stable():
    fp1 = compute_fingerprint(1, date(2026, 5, 1), -12345, "Biedronka")
    fp2 = compute_fingerprint(1, date(2026, 5, 1), -12345, "Biedronka")
    fp3 = compute_fingerprint(1, date(2026, 5, 1), -12346, "Biedronka")
    assert fp1 == fp2
    assert fp1 != fp3
    assert len(fp1) == 64  # sha256 hex
