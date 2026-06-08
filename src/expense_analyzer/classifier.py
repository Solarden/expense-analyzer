"""Categorization classifier — layer 2, the text model (design §7.7 point 2).

Pure logic, zero DB (mirrors :mod:`expense_analyzer.rules` /
:mod:`expense_analyzer.transfers` / :mod:`expense_analyzer.subscriptions`). A
TF-IDF + logistic-regression classifier over a transaction's merchant (or, when
none was extracted, its raw description). It learns from already-categorized
transactions and predicts a category **with a probability** for the uncategorized
ones — so a confident prediction can be auto-applied and an unsure one routed to
the manual review queue. That confidence gate is the active-learning loop: tag a
queued row and it becomes training data for the next run.

The model is trained **fresh on each run** (see :func:`train`) from confirmed
labels; there is no stored model file. Fitting TF-IDF + logistic regression over a
household's history takes well under a second, so a pickled model, a retrain
cadence and staleness are all avoidable (recompute-live, like the subscription
detector). The DB side — which rows train, which get classified, and the queue —
lives in :mod:`expense_analyzer.queries.classifier`.

scikit-learn (and its numpy/scipy stack) is imported lazily inside :func:`train`,
not at module load, so merely importing this module — or the import pipeline that
depends on it — doesn't pull ~100 MB of heavy libraries into a process that may
never classify anything (e.g. app boot before the first import or queue visit).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sklearn.pipeline import Pipeline

# A logistic regression needs at least two classes to fit; with one category there
# is nothing to discriminate, so training is a no-op below this.
MIN_DISTINCT_CATEGORIES = 2


def build_text(merchant_normalized: str | None, raw_description: str) -> str:
    """The text the classifier learns from and predicts on.

    The normalized merchant when present, otherwise the raw bank description — the
    same field preference a rule uses (:mod:`expense_analyzer.rules`), so the two
    layers reason about the same string."""
    return (merchant_normalized or raw_description or "").strip()


@dataclass(frozen=True, slots=True)
class TrainingSample:
    """One labeled example: the feature text and the category it belongs to.
    Decoupled from the ORM so the model stays pure and testable without a session."""

    text: str
    category_id: int


@dataclass(frozen=True, slots=True)
class Prediction:
    """The classifier's verdict for one transaction: the most likely category and
    the model's probability for it (0..1), used as the auto-apply confidence."""

    category_id: int
    confidence: float


class Classifier:
    """A fitted text classifier. Construct via :func:`train` (which returns ``None``
    when there isn't enough labeled data to learn from)."""

    def __init__(self, pipeline: Pipeline) -> None:
        self._pipeline = pipeline

    def predict(self, text: str) -> Prediction | None:
        """The most probable category for ``text`` and its probability, or ``None``
        for empty text (nothing to go on)."""
        return self.predict_batch([text])[0]

    def predict_batch(self, texts: Sequence[str]) -> list[Prediction | None]:
        """Predictions for ``texts`` in a single vectorize + predict pass, aligned to
        the input order. An empty text maps to ``None`` (nothing to go on) and is
        excluded from the model call rather than fed a zero vector."""
        stripped = [t.strip() for t in texts]
        results: list[Prediction | None] = [None] * len(stripped)

        nonempty = [i for i, t in enumerate(stripped) if t]
        if not nonempty:
            return results

        proba = self._pipeline.predict_proba([stripped[i] for i in nonempty])
        classes = self._pipeline.classes_
        for slot, row in zip(nonempty, proba, strict=True):
            best = int(row.argmax())
            results[slot] = Prediction(category_id=int(classes[best]), confidence=float(row[best]))

        return results


def train(samples: Sequence[TrainingSample], *, min_samples: int) -> Classifier | None:
    """Fit a classifier on ``samples`` (feature text + category label).

    Returns ``None`` when there is too little to learn from — fewer than
    ``min_samples`` usable rows, or fewer than :data:`MIN_DISTINCT_CATEGORIES`
    distinct categories. Rows with blank text are dropped first (they carry no
    signal). The caller treats ``None`` as a cold start: classify nothing, leave
    rows in the queue.

    ``random_state`` is pinned so a given training set always yields the same model
    (and the same predictions) — deterministic, like the rest of the pipeline.
    """
    usable = [s for s in samples if s.text.strip()]
    if len(usable) < max(min_samples, MIN_DISTINCT_CATEGORIES):
        return None
    if len({s.category_id for s in usable}) < MIN_DISTINCT_CATEGORIES:
        return None

    # Imported here, not at module top: this is the only place that constructs
    # sklearn objects, so deferring the import keeps the heavy numpy/scipy/sklearn
    # stack off any process that imports this module but never trains.
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline

    pipeline = Pipeline(
        [
            # Word unigrams + bigrams over the lowercased text. Bank descriptions
            # are short and repetitive, so this captures merchant tokens cheaply.
            ("tfidf", TfidfVectorizer(lowercase=True, ngram_range=(1, 2))),
            ("clf", LogisticRegression(max_iter=1000, random_state=0)),
        ]
    )
    pipeline.fit([s.text for s in usable], [s.category_id for s in usable])

    return Classifier(pipeline)
