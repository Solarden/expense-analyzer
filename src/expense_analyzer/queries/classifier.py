"""Classifier queries — the DB side of categorization layer 2 (design §7.7 point 2).

Selects the training set, runs the pure model (:mod:`expense_analyzer.classifier`)
over candidate transactions, and either writes a confident prediction back or
leaves the row for the manual review queue. Applied both at import time (the new
rows, via the import pipeline, after the deterministic rules) and on demand from
the queue page ("Train & classify now").

Layered precedence — manual > rule > classifier > none:

- **Trains only on confirmed labels** (``source in {manual, rule}``, expense/income
  categories). The classifier never trains on its *own* past guesses
  (``source = classifier``), so it can't reinforce its mistakes; a human or a rule
  has to vouch for a label before it teaches the model.
- **Classifies only untouched uncategorized rows** (``category_id IS NULL`` and
  ``source = import_csv``) — exactly the rows a rule would take. A manual
  categorization (even one a human deliberately *cleared*), a rule's, and the
  classifier's own earlier output are all left alone.
- A confident prediction (``>= classifier_confidence_threshold``) sets
  ``category_id``, ``source = classifier`` and ``confidence`` to the probability.
  Below the threshold the row stays uncategorized and surfaces in the queue.
"""

from dataclasses import dataclass

from sqlalchemy import func
from sqlmodel import Session, col, select

from expense_analyzer.classifier import Classifier, Prediction, TrainingSample, build_text, train
from expense_analyzer.config import Settings, get_settings
from expense_analyzer.models import Category, CategoryKind, Transaction, TxSource

# Categories the classifier learns and predicts. Transfers are handled by transfer
# linking, not categorization, so a transfer-kind category is neither a label nor a
# prediction target (same exclusion the rules layer makes).
_LEARNABLE_KINDS = (CategoryKind.expense, CategoryKind.income)


@dataclass(frozen=True, slots=True)
class ClassifyResult:
    """Outcome of a classify run.

    ``trained`` is ``False`` on a cold start (too few confirmed labels, or fewer
    than two categories) — nothing was classified and every candidate is waiting in
    the queue.
    """

    categorized: int  # rows auto-tagged (prediction at/above the confidence threshold)
    queued: int  # candidates whose best prediction was below the threshold (left for review)
    candidates: int  # eligible uncategorized rows considered
    trained: bool  # False => cold start (model couldn't be trained), nothing changed


@dataclass(frozen=True, slots=True)
class QueueRow:
    """A queued (uncategorized) transaction plus the classifier's suggestion for it,
    shown on the review-queue page. ``suggestion`` is ``None`` on a cold start or for
    empty text."""

    transaction: Transaction
    suggestion: Prediction | None


@dataclass(frozen=True, slots=True)
class QueuePage:
    """One page of the review queue (uncategorized rows, newest first), plus whether
    the model could be trained (so the page can explain a cold start)."""

    rows: list[QueueRow]
    total: int
    page: int
    page_size: int
    trained: bool

    @property
    def pages(self) -> int:
        size = max(1, self.page_size)
        return max(1, -(-self.total // size))  # ceil div, never 0

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def _learnable_category_ids(session: Session) -> set[int]:
    """Ids of expense/income categories — the only labels/targets the classifier uses."""
    rows = session.exec(select(Category).where(col(Category.kind).in_(_LEARNABLE_KINDS))).all()

    return {c.id for c in rows if c.id is not None}


def _training_samples(session: Session) -> list[TrainingSample]:
    """Confirmed labels to learn from: non-deleted rows with an expense/income
    category set by a human or a rule (``source in {manual, rule}``). The
    classifier's own guesses are excluded so it never trains on its own output."""
    learnable = _learnable_category_ids(session)
    if not learnable:
        return []

    rows = session.exec(
        select(Transaction).where(
            col(Transaction.deleted_at).is_(None),
            col(Transaction.category_id).in_(learnable),
            col(Transaction.source).in_([TxSource.manual, TxSource.rule]),
        )
    ).all()

    return [
        TrainingSample(
            text=build_text(r.merchant_normalized, r.raw_description),
            category_id=r.category_id,
        )
        for r in rows
        if r.category_id is not None
    ]


def _candidate_filter(query):
    """Untouched uncategorized rows — the only ones the classifier may tag (the same
    eligibility the rules layer's uncategorized branch uses)."""
    return query.where(
        col(Transaction.deleted_at).is_(None),
        col(Transaction.category_id).is_(None),
        Transaction.source == TxSource.import_csv,
    )


def _train_model(session: Session, settings: Settings) -> Classifier | None:
    return train(_training_samples(session), min_samples=settings.classifier_min_training_samples)


def classify(session: Session, *, settings: Settings | None = None) -> ClassifyResult:
    """Train on confirmed labels and auto-categorize confident candidates.

    Returns a :class:`ClassifyResult`. A confident prediction
    (``>= classifier_confidence_threshold``) is written back as
    ``source = classifier``; a less confident one leaves the row uncategorized for
    the review queue. On a cold start (the model can't be trained) nothing changes.
    """
    settings = settings or get_settings()
    candidates = list(session.exec(_candidate_filter(select(Transaction))).all())

    model = _train_model(session, settings)
    if model is None:
        return ClassifyResult(
            categorized=0, queued=len(candidates), candidates=len(candidates), trained=False
        )

    threshold = settings.classifier_confidence_threshold
    predictions = model.predict_batch(
        [build_text(tx.merchant_normalized, tx.raw_description) for tx in candidates]
    )
    categorized = 0
    queued = 0
    for tx, prediction in zip(candidates, predictions, strict=True):
        if prediction is None or prediction.confidence < threshold:
            queued += 1
            continue
        tx.category_id = prediction.category_id
        tx.source = TxSource.classifier
        tx.confidence = prediction.confidence
        session.add(tx)
        categorized += 1

    session.commit()

    return ClassifyResult(
        categorized=categorized, queued=queued, candidates=len(candidates), trained=True
    )


def review_queue(
    session: Session, *, page: int, page_size: int, settings: Settings | None = None
) -> QueuePage:
    """One page of uncategorized transactions (newest first) with the classifier's
    suggestion attached to each. The model is trained once for the whole page;
    ``trained`` is ``False`` on a cold start, in which case suggestions are ``None``.
    """
    settings = settings or get_settings()
    page = max(1, page)

    total = session.exec(_candidate_filter(select(func.count()).select_from(Transaction))).one()

    rows = session.exec(
        _candidate_filter(select(Transaction))
        .order_by(col(Transaction.booked_date).desc(), col(Transaction.id).desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    # Train only when there's a page of rows to suggest for — an empty page (queue
    # cleared, or paged past the end) shouldn't pay to fit a model it won't use.
    model = _train_model(session, settings) if rows else None
    suggestions: list[Prediction | None] = (
        model.predict_batch([build_text(tx.merchant_normalized, tx.raw_description) for tx in rows])
        if model is not None
        else [None] * len(rows)
    )
    queue_rows = [
        QueueRow(transaction=tx, suggestion=s) for tx, s in zip(rows, suggestions, strict=True)
    ]

    return QueuePage(
        rows=queue_rows,
        total=int(total),
        page=page,
        page_size=page_size,
        trained=model is not None,
    )
