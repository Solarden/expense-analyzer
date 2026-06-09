"""Embeddings neighbours — the DB side of categorization layer 3 (design §7.7 point 3).

Builds a nearest-neighbour index from the same confirmed labels the classifier
trains on (:func:`expense_analyzer.queries.categorize.classifier.confirmed_label_texts`), then
finds, for each transaction in the review queue, the most similar past
categorization. The result decorates the queue page next to the classifier's guess
— it is **never written back** (no auto-apply, no new ``TxSource``); a human still
confirms the tag. See :mod:`expense_analyzer.embeddings` for the pure logic.

Fail-safe by construction: the embedding model is heavy and bundled at build time,
so anything that prevents producing suggestions — the feature disabled, a cold
start (too few labels), or the model failing to load (e.g. missing weights, or an
offline process with nothing cached) — yields **no suggestions** and a clean queue
render, never an error. The expensive pieces (the loaded model, the embedded
training matrix) are cached so repeated queue loads don't re-embed history.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlmodel import Session

from expense_analyzer.config import Settings, get_settings
from expense_analyzer.embeddings import (
    Embedder,
    NeighborModel,
    NeighborSuggestion,
    TrainingSample,
    build,
    build_text,
    load_embedder,
)
from expense_analyzer.models import Transaction
from expense_analyzer.queries.categorize.classifier import confirmed_label_texts

log = logging.getLogger(__name__)

# Single-entry cache of the embedded training index, keyed on (model@revision,
# label-set fingerprint). Embedding a household's whole history is the costly step,
# so we keep the last-built index and rebuild only when the confirmed labels change
# (e.g. after tagging a queued row). Only used for the real (process-loaded)
# embedder — an injected one (tests) bypasses the cache so a fake never shadows
# another. A cold start (build -> None) isn't cached: that path returns before
# embedding anything, so re-running it is cheap.
_index_cache: dict[tuple[str, int], NeighborModel] = {}


def _fingerprint(samples: Sequence[TrainingSample]) -> int:
    return hash(tuple(sorted((s.text, s.category_id) for s in samples)))


def _index(
    samples: Sequence[TrainingSample],
    embedder: Embedder,
    *,
    settings: Settings,
    cache_key: str | None,
) -> NeighborModel | None:
    global _index_cache

    min_samples = settings.embeddings_min_training_samples
    if cache_key is None:
        return build(samples, embedder, min_samples=min_samples)

    key = (cache_key, _fingerprint(samples))
    cached = _index_cache.get(key)
    if cached is not None:
        return cached

    model = build(samples, embedder, min_samples=min_samples)
    if model is not None:
        # Replace the whole dict (single entry, latest label set only). Rebinding the
        # module global is atomic under the GIL, so a concurrent reader sees either
        # the old map or the new one — never a half-cleared one (the clear()-then-set
        # version could KeyError a racing reader).
        _index_cache = {key: model}

    return model


def neighbor_suggestions(
    session: Session,
    transactions: Sequence[Transaction],
    *,
    settings: Settings | None = None,
    embedder: Embedder | None = None,
) -> dict[int, NeighborSuggestion]:
    """The kNN suggestion for each of ``transactions``, keyed by transaction id.

    Only rows with a useful match appear in the result — a transaction with blank
    text, or whose nearest labelled neighbour is below
    ``embeddings_min_similarity``, is simply absent (the queue shows no layer-3 hint
    for it). Returns ``{}`` when the feature is off, on a cold start, or if the model
    can't be loaded; never raises, so the queue always renders.
    """
    settings = settings or get_settings()
    if not settings.embeddings_enabled:
        return {}

    try:
        cache_key: str | None = None
        if embedder is None:
            embedder = load_embedder(settings.embeddings_model, settings.embeddings_model_revision)
            cache_key = f"{settings.embeddings_model}@{settings.embeddings_model_revision}"

        samples = [TrainingSample(text=t, category_id=c) for t, c in confirmed_label_texts(session)]
        model = _index(samples, embedder, settings=settings, cache_key=cache_key)
        if model is None:
            return {}

        # Skip blank-text rows (nothing to embed) and pin each query to its tx id.
        queries = [
            (tx.id, build_text(tx.merchant_normalized, tx.raw_description))
            for tx in transactions
            if tx.id is not None
        ]
        queries = [(tid, text) for tid, text in queries if text]
        if not queries:
            return {}

        suggestions = model.suggest_batch(
            embedder.encode([text for _, text in queries]),
            k=settings.embeddings_neighbors,
            min_similarity=settings.embeddings_min_similarity,
        )
    except Exception:  # noqa: BLE001 — suggestions are a convenience; never break the queue
        log.exception("embeddings neighbour suggestions failed; rendering queue without them")
        return {}

    return {
        tid: suggestion
        for (tid, _), suggestion in zip(queries, suggestions, strict=True)
        if suggestion is not None
    }
