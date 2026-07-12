"""Tests for the bank-agnostic import core: idempotent upsert and rollback.

Uses a fake in-memory importer (``make_importer`` from conftest) so these cover
the pipeline itself, independent of any specific bank's CSV format.
"""

from collections.abc import Callable
from datetime import date

from sqlmodel import Session, select

from expense_analyzer.importers import (
    Importer,
    NormalizedTransaction,
    compute_fingerprint,
    rollback_batch,
    run_import,
)
from expense_analyzer.models import Account, ImportBatch, ImportStatus, Transaction, TxSource
from expense_analyzer.queries.core import users


def _records() -> list[NormalizedTransaction]:
    return [
        NormalizedTransaction(date(2026, 5, 1), -12345, "Biedronka", balance_after=500000),
        NormalizedTransaction(date(2026, 5, 2), 1000000, "Wyplata", balance_after=1500000),
        NormalizedTransaction(date(2026, 5, 3), -4999, "Spotify"),
    ]


def test_import_inserts_new_transactions(
    db_session: Session, account: Account, make_importer: Callable[..., Importer]
):
    summary = run_import(
        db_session,
        account_id=account.id,
        importer=make_importer(_records()),
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


def test_import_stamps_owner_id(
    db_session: Session, account: Account, make_importer: Callable[..., Importer]
):
    """Imported rows carry the uploading user's id (provenance for per-user scoping)."""
    alice = users.create_user(db_session, username="alice", name="Alice", password="secret123")
    run_import(
        db_session,
        account_id=account.id,
        importer=make_importer(_records()),
        filename="may.csv",
        data=b"",
        owner_id=alice.id,
    )

    rows = db_session.exec(select(Transaction)).all()
    assert rows and all(tx.owner_id == alice.id for tx in rows)


def test_reimport_same_file_is_idempotent(
    db_session: Session, account: Account, make_importer: Callable[..., Importer]
):
    importer = make_importer(_records())
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


def test_overlapping_import_inserts_only_the_new_rows(
    db_session: Session, account: Account, make_importer: Callable[..., Importer]
):
    run_import(
        db_session,
        account_id=account.id,
        importer=make_importer(_records()[:2]),
        filename="d1.csv",
        data=b"",
    )
    # A later export overlaps the first two rows and adds one more.
    summary = run_import(
        db_session,
        account_id=account.id,
        importer=make_importer(_records()),
        filename="d2.csv",
        data=b"",
    )
    assert summary.new == 1
    assert summary.skipped == 2
    assert len(db_session.exec(select(Transaction)).all()) == 3


def test_fingerprint_is_account_scoped(
    db_session: Session,
    account: Account,
    make_account: Callable[..., Account],
    make_importer: Callable[..., Importer],
):
    other = make_account(name="mBank")

    rec = _records()[:1]
    run_import(
        db_session, account_id=account.id, importer=make_importer(rec), filename="a.csv", data=b""
    )
    summary = run_import(
        db_session, account_id=other.id, importer=make_importer(rec), filename="b.csv", data=b""
    )
    # Same row, different account -> not a duplicate.
    assert summary.new == 1
    assert len(db_session.exec(select(Transaction)).all()) == 2


def test_rollback_soft_deletes_batch(
    db_session: Session, account: Account, make_importer: Callable[..., Importer]
):
    summary = run_import(
        db_session,
        account_id=account.id,
        importer=make_importer(_records()),
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


def test_in_file_duplicate_is_imported_once(
    db_session: Session, account: Account, make_importer: Callable[..., Importer]
):
    # Two genuinely-identical rows in one file collapse to one fingerprint. The
    # second must be skipped (design's accepted behaviour) without tripping the
    # unique index — i.e. dedup happens within the run, not only against the DB.
    dup = NormalizedTransaction(date(2026, 5, 1), -500, "Kawa")
    summary = run_import(
        db_session,
        account_id=account.id,
        importer=make_importer([dup, dup]),
        filename="dup.csv",
        data=b"",
    )

    assert summary.parsed == 2
    assert summary.new == 1
    assert summary.skipped == 1
    assert len(db_session.exec(select(Transaction)).all()) == 1


def test_pipeline_fills_merchant_normalized(
    db_session: Session, account: Account, make_importer: Callable[..., Importer]
):
    rec = NormalizedTransaction(
        date(2026, 5, 1),
        -4240,
        "Płatność kartą | Lokalizacja: Adres: Testowy Sklep Miasto: Łódź Kraj: POLSKA",
    )
    run_import(
        db_session, account_id=account.id, importer=make_importer([rec]), filename="m.csv", data=b""
    )

    tx = db_session.exec(select(Transaction)).one()
    assert tx.merchant_normalized == "TESTOWY SKLEP"


def test_summary_carries_reconciliation_from_declared_totals(
    db_session: Session, account: Account, make_importer: Callable[..., Importer]
):
    # _records()[:2] sums to +1_000_000 inflow and -12_345 outflow.
    importer = make_importer(_records()[:2], declared_inflow=1_000_000, declared_outflow=-12345)
    summary = run_import(
        db_session, account_id=account.id, importer=importer, filename="r.csv", data=b""
    )

    assert summary.reconciliation.ok
    assert summary.reconciliation.label == "OK"


def test_summary_reconciliation_flags_mismatch(
    db_session: Session, account: Account, make_importer: Callable[..., Importer]
):
    importer = make_importer(
        [NormalizedTransaction(date(2026, 5, 1), 5000, "Wpływ")],
        declared_inflow=9999,  # parser says 5000, bank declared 9999 -> mismatch
    )
    summary = run_import(
        db_session, account_id=account.id, importer=importer, filename="bad.csv", data=b""
    )

    assert not summary.reconciliation.ok
    assert summary.reconciliation.label == "Mismatch"


def test_import_auto_links_cross_account_transfer(
    db_session: Session,
    account: Account,
    make_account: Callable[..., Account],
    make_importer: Callable[..., Importer],
):
    # Imports stamp the uploader as owner and auto-link runs as that viewer, so a
    # user's own (default-private) transfer legs still pair — mirroring production,
    # where the upload endpoint passes owner_id=user.id.
    alice = users.create_user(db_session, username="alice", name="Alice", password="pw")
    other = make_account(name="mBank")

    # Outflow lands on the first account; its equal-and-opposite counterpart
    # arrives in a later import on the other account.
    run_import(
        db_session,
        account_id=account.id,
        importer=make_importer([NormalizedTransaction(date(2026, 5, 1), -200000, "Transfer out")]),
        filename="a.csv",
        data=b"",
        owner_id=alice.id,
    )
    summary = run_import(
        db_session,
        account_id=other.id,
        importer=make_importer([NormalizedTransaction(date(2026, 5, 2), 200000, "Transfer in")]),
        filename="b.csv",
        data=b"",
        owner_id=alice.id,
    )

    assert summary.transfers_auto_linked == 1
    groups = {tx.transfer_group_id for tx in db_session.exec(select(Transaction)).all()}
    assert groups != {None}
    assert len(groups) == 1  # both legs share one group id


def test_reimport_with_nothing_new_skips_transfer_detection(
    db_session: Session,
    account: Account,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., Transaction],
    make_importer: Callable[..., Importer],
):
    # An importable file with a single outflow; first import has nothing to pair.
    importer = make_importer([NormalizedTransaction(date(2026, 5, 1), -200000, "Transfer out")])
    run_import(db_session, account_id=account.id, importer=importer, filename="a.csv", data=b"")

    # A pairable counterpart now exists on another account (inserted directly,
    # left unmatched) — detection *would* link it if it ran.
    other = make_account(name="mBank")
    counterpart = make_transaction(
        account_id=other.id, amount=200000, day=2, raw_description="Transfer in"
    )

    # Re-importing the same file is all-duplicates (new == 0), so the `if new:`
    # guard skips detection entirely — the pair is left unlinked.
    summary = run_import(
        db_session, account_id=account.id, importer=importer, filename="a.csv", data=b""
    )
    assert summary.new == 0
    assert summary.transfers_auto_linked == 0
    db_session.refresh(counterpart)
    assert counterpart.transfer_group_id is None


def test_compute_fingerprint_is_stable():
    fp1 = compute_fingerprint(1, date(2026, 5, 1), -12345, "Biedronka")
    fp2 = compute_fingerprint(1, date(2026, 5, 1), -12345, "Biedronka")
    fp3 = compute_fingerprint(1, date(2026, 5, 1), -12346, "Biedronka")
    assert fp1 == fp2
    assert fp1 != fp3
    assert len(fp1) == 64  # sha256 hex
