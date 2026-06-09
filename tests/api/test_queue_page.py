"""Classifier (layer 2): query layer, review-queue page, and the import-time hook
(Phase 11, design §7.7 point 2).

HTTP tests use ``auth_client`` (logged in), sharing the temp engine with
``db_session``. The query-layer tests inject a :class:`Settings` with a low
training floor / threshold so a tiny fixture set is enough to train; the
import-hook and endpoint tests run against the real defaults.
"""

from collections.abc import Callable
from datetime import date

from fastapi import status
from fastapi.testclient import TestClient
from sqlmodel import Session

from expense_analyzer.config import Settings
from expense_analyzer.importers import NormalizedTransaction
from expense_analyzer.importers.pipeline import run_import
from expense_analyzer.models import Account, Category, Scope, Transaction, TxSource
from expense_analyzer.queries.categorize import classifier as cq

# A permissive config so a handful of fixture rows is enough to train.
_LOW = Settings(classifier_min_training_samples=4, classifier_confidence_threshold=0.5)


def _seed_labels(
    make_transaction: Callable[..., Transaction],
    *,
    account_id: int,
    food_id: int,
    fun_id: int,
    n_each: int = 6,
    source: TxSource = TxSource.manual,
) -> None:
    """A clean two-class confirmed-label set: BIEDRONKA->Food, NETFLIX->Fun."""
    for _ in range(n_each):
        make_transaction(
            account_id=account_id,
            amount=-5000,
            merchant_normalized="BIEDRONKA",
            category_id=food_id,
            source=source,
        )
        make_transaction(
            account_id=account_id,
            amount=-3000,
            merchant_normalized="NETFLIX",
            category_id=fun_id,
            source=source,
        )


# --- query layer ----------------------------------------------------------


def test_classify_cold_start_leaves_everything_for_the_queue(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
) -> None:
    # No labels at all -> the model can't be trained; the candidate is untouched.
    tx = make_transaction(account_id=account.id, amount=-5000, merchant_normalized="BIEDRONKA")

    result = cq.classify(db_session, settings=_LOW)

    assert result.trained is False
    assert result.categorized == 0
    assert result.candidates == 1
    db_session.refresh(tx)
    assert tx.category_id is None
    assert tx.source == TxSource.import_csv


def test_classify_auto_applies_confident_prediction(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    _seed_labels(make_transaction, account_id=account.id, food_id=food.id, fun_id=fun.id)
    tx = make_transaction(account_id=account.id, amount=-5000, merchant_normalized="BIEDRONKA 12")

    result = cq.classify(db_session, settings=_LOW)

    assert result.trained is True
    assert result.categorized == 1
    db_session.refresh(tx)
    assert tx.category_id == food.id
    assert tx.source == TxSource.classifier
    assert tx.confidence is not None and 0.5 <= tx.confidence <= 1.0


def test_classify_leaves_low_confidence_in_queue(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    _seed_labels(make_transaction, account_id=account.id, food_id=food.id, fun_id=fun.id)
    tx = make_transaction(account_id=account.id, amount=-5000, merchant_normalized="BIEDRONKA 12")

    # An unreachable threshold -> a confident-but-not-certain guess stays queued.
    strict = Settings(classifier_min_training_samples=4, classifier_confidence_threshold=0.9999)
    result = cq.classify(db_session, settings=strict)

    assert result.categorized == 0
    assert result.queued == 1
    db_session.refresh(tx)
    assert tx.category_id is None
    assert tx.source == TxSource.import_csv


def test_classify_never_touches_manual_or_rule_rows(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    other = make_category(name="Other")
    _seed_labels(make_transaction, account_id=account.id, food_id=food.id, fun_id=fun.id)
    # A human's verdict and a rule's verdict on rows the model would happily relabel.
    manual = make_transaction(
        account_id=account.id,
        amount=-5000,
        merchant_normalized="BIEDRONKA",
        category_id=other.id,
        source=TxSource.manual,
    )
    ruled = make_transaction(
        account_id=account.id,
        amount=-5000,
        merchant_normalized="BIEDRONKA",
        category_id=other.id,
        source=TxSource.rule,
    )

    cq.classify(db_session, settings=_LOW)

    db_session.refresh(manual)
    db_session.refresh(ruled)
    assert (manual.category_id, manual.source) == (other.id, TxSource.manual)
    assert (ruled.category_id, ruled.source) == (other.id, TxSource.rule)


def test_classifier_does_not_train_on_its_own_output(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    # 10 classifier-sourced FOOD rows + 3 manual FUN rows, floor 8. If the
    # classifier's own guesses counted, that's 13 rows / 2 classes -> trained.
    # They must NOT: only the 3 manual rows are usable -> cold start.
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    for _ in range(10):
        make_transaction(
            account_id=account.id,
            amount=-5000,
            merchant_normalized="BIEDRONKA",
            category_id=food.id,
            source=TxSource.classifier,
        )
    for _ in range(3):
        make_transaction(
            account_id=account.id,
            amount=-3000,
            merchant_normalized="NETFLIX",
            category_id=fun.id,
            source=TxSource.manual,
        )

    floor8 = Settings(classifier_min_training_samples=8, classifier_confidence_threshold=0.5)
    result = cq.classify(db_session, settings=floor8)

    assert result.trained is False


def test_review_queue_lists_uncategorized_with_suggestions(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    _seed_labels(make_transaction, account_id=account.id, food_id=food.id, fun_id=fun.id)
    candidate = make_transaction(
        account_id=account.id, amount=-5000, merchant_normalized="BIEDRONKA 12"
    )

    queue = cq.review_queue(db_session, page=1, page_size=50, settings=_LOW)

    assert queue.trained is True
    assert queue.total == 1
    [row] = queue.rows
    assert row.transaction.id == candidate.id
    assert row.suggestion is not None
    assert row.suggestion.category_id == food.id


def test_review_queue_excludes_non_candidates(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    # Only the untouched, uncategorized import row is a candidate.
    keep = make_transaction(account_id=account.id, amount=-5000, merchant_normalized="UNKNOWN SHOP")
    make_transaction(  # already categorized
        account_id=account.id, amount=-100, category_id=food.id, source=TxSource.classifier
    )
    make_transaction(  # manually cleared
        account_id=account.id, amount=-200, category_id=None, source=TxSource.manual
    )
    make_transaction(  # soft-deleted
        account_id=account.id, amount=-300, deleted_at=date(2026, 5, 1)
    )

    queue = cq.review_queue(db_session, page=1, page_size=50, settings=_LOW)

    assert [r.transaction.id for r in queue.rows] == [keep.id]


# --- import-time hook -----------------------------------------------------


def test_import_hook_is_a_noop_on_cold_start(
    db_session: Session,
    account: Account,
    make_importer: Callable[..., object],
) -> None:
    # Far fewer than the default 25-label floor -> classifier stays cold, and the
    # import still succeeds reporting auto_classified == 0 (non-fatal contract).
    importer = make_importer([NormalizedTransaction(date(2026, 5, 1), -5000, "BIEDRONKA 99")])
    summary = run_import(
        db_session, account_id=account.id, importer=importer, filename="x.csv", data=b""
    )

    assert summary.new == 1
    assert summary.auto_classified == 0


def test_import_auto_classifies_with_enough_labels(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
    make_importer: Callable[..., object],
) -> None:
    # 13 + 13 = 26 confirmed labels clears the default floor (25); a new BIEDRONKA
    # row is confidently classified by the import hook at the real default threshold.
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    _seed_labels(make_transaction, account_id=account.id, food_id=food.id, fun_id=fun.id, n_each=13)

    importer = make_importer([NormalizedTransaction(date(2026, 5, 1), -5000, "BIEDRONKA 99")])
    summary = run_import(
        db_session, account_id=account.id, importer=importer, filename="x.csv", data=b""
    )

    assert summary.new == 1
    assert summary.auto_classified == 1


# --- endpoints ------------------------------------------------------------


def test_queue_page_renders(auth_client: TestClient) -> None:
    resp = auth_client.get("/dashboard/queue")
    assert resp.status_code == status.HTTP_200_OK
    assert "Review queue" in resp.text


def test_classify_endpoint_redirects_with_counts(auth_client: TestClient) -> None:
    resp = auth_client.post("/dashboard/queue/classify", follow_redirects=False)

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    # No labels -> cold start (trained=0); the follow-up GET explains it.
    assert resp.headers["location"] == "/dashboard/queue?categorized=0&queued=0&trained=0"
    page = auth_client.get(resp.headers["location"])
    assert "Not enough categorized transactions" in page.text


def test_categorize_from_queue_marks_manual(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    tx = make_transaction(account_id=account.id, amount=-5000, merchant_normalized="BIEDRONKA")

    resp = auth_client.post(
        f"/dashboard/queue/{tx.id}/categorize?page=1",
        data={"category_id": str(food.id), "scope": Scope.household.value},
        follow_redirects=False,
    )

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"] == "/dashboard/queue"
    db_session.refresh(tx)
    assert tx.category_id == food.id
    assert tx.source == TxSource.manual
    assert tx.scope == Scope.household
