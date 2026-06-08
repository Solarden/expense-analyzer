"""Categorization neighbours — layer 3, the embeddings fallback (design §7.7 point 3).

Pure logic, zero DB (mirrors :mod:`expense_analyzer.classifier` /
:mod:`expense_analyzer.rules`). Where layer 1 (rules) and layer 2 (the TF-IDF +
logistic-regression classifier) reason about *tokens*, this layer reasons about
*meaning*: it embeds each transaction's text into a vector and finds the nearest
already-categorized transactions by cosine similarity (a kNN vote). That catches
the "weird cases" the design earmarks for layer 3 — a never-before-seen merchant
whose description is semantically close to ones you've tagged, where a substring
rule has nothing to match and the classifier has no confident token overlap.

Deliberately a **suggestion only** — it never writes a category back. Its output
decorates the review queue next to the classifier's guess (the nearest past
example and how similar it is), and a human still confirms the tag (``source =
manual``). So there is no new ``TxSource``, no auto-apply, no migration: layer 3 is
purely additive on top of layers 1–2.

The embedding model is **heavy** (sentence-transformers pulls torch) and the model
weights are baked into the Docker image at build time, never fetched at runtime —
so this module imports sentence-transformers lazily inside :func:`load_embedder`,
and the DB layer fails safe (no suggestions, queue still renders) when the model
can't be loaded. The :class:`Embedder` protocol is injectable so the logic is
testable with a tiny deterministic fake — no torch, no model download in the suite.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

# A kNN vote needs at least two classes to choose between, and enough labelled
# history for "nearest" to mean anything — same cold-start floor the classifier
# uses (queries inject the configured value; this is the hard minimum).
MIN_DISTINCT_CATEGORIES = 2


def build_text(merchant_normalized: str | None, raw_description: str) -> str:
    """The text embedded for similarity — the normalized merchant when present,
    otherwise the raw description. The same field preference the rules and the
    classifier use, so all three layers reason about the same string."""
    return (merchant_normalized or raw_description or "").strip()


class Embedder(Protocol):
    """Turns texts into a row-per-text matrix of **L2-normalized** vectors, so a dot
    product is the cosine similarity. Injectable: the real one wraps a
    sentence-transformers model (:func:`load_embedder`); tests pass a fake."""

    def encode(self, texts: Sequence[str]) -> NDArray[np.float32]: ...


@dataclass(frozen=True, slots=True)
class TrainingSample:
    """One labelled example: the feature text and its category. Decoupled from the
    ORM so the model stays pure and testable without a session (mirrors the
    classifier's sample type)."""

    text: str
    category_id: int


@dataclass(frozen=True, slots=True)
class NeighborSuggestion:
    """The kNN verdict for one transaction: the category its nearest labelled
    neighbours vote for, the cosine similarity of the closest neighbour backing that
    category (0..1), and that neighbour's text — shown in the queue so a human sees
    *why* (\"similar to BIEDRONKA 77\"), not just a bare label."""

    category_id: int
    similarity: float
    example_text: str


class NeighborModel:
    """A fitted nearest-neighbour index over labelled transactions. Construct via
    :func:`build` (which returns ``None`` on a cold start)."""

    def __init__(self, matrix: NDArray[np.float32], labels: list[int], texts: list[str]) -> None:
        self._matrix = matrix  # N×D, L2-normalized
        self._labels = labels
        self._texts = texts

    def suggest_batch(
        self, query_matrix: NDArray[np.float32], *, k: int, min_similarity: float
    ) -> list[NeighborSuggestion | None]:
        """A suggestion per query row (aligned to input order), or ``None`` when the
        closest neighbour is below ``min_similarity`` (too far to be a useful hint).

        Each row votes among its ``k`` nearest labelled neighbours: the category with
        the most votes wins (ties broken by the single closest neighbour). The
        reported similarity and example are that winning category's closest neighbour.
        """
        if query_matrix.shape[0] == 0:
            return []

        # Cosine similarity for every (query, sample) pair at once — both sides are
        # L2-normalized, so the dot product is the cosine. M×N.
        sims = query_matrix @ self._matrix.T
        neighbours = min(k, self._matrix.shape[0])

        results: list[NeighborSuggestion | None] = []
        for row in sims:
            # Indices of the top-`neighbours` samples, most similar first. argpartition
            # finds the k best in O(N) (no full sort of every sample); then we sort
            # just those k descending — k is tiny, so this is cheaper than argsort(N).
            part = np.argpartition(row, -neighbours)[-neighbours:]
            top = part[np.argsort(row[part])[::-1]]
            if row[top[0]] < min_similarity:
                results.append(None)
                continue

            votes: dict[int, int] = defaultdict(int)
            for idx in top:
                votes[self._labels[idx]] += 1
            # Winning category: most votes, ties broken by the closest neighbour
            # (top is already sorted by similarity, so the first hit wins the tie).
            winner = max(votes, key=lambda c: (votes[c], -_first_index(self._labels, top, c)))

            # The closest neighbour that actually backs the winning category — its
            # similarity and text are what we surface.
            best = next(idx for idx in top if self._labels[idx] == winner)
            results.append(
                NeighborSuggestion(
                    category_id=winner,
                    similarity=float(row[best]),
                    example_text=self._texts[best],
                )
            )

        return results


def _first_index(labels: list[int], order: NDArray[np.intp], category: int) -> int:
    """Rank (0 = closest) of the first neighbour in ``order`` whose label is
    ``category`` — the tie-break key so the category with the nearer neighbour wins."""
    for rank, idx in enumerate(order):
        if labels[idx] == category:
            return rank

    return len(order)


def build(
    samples: Sequence[TrainingSample], embedder: Embedder, *, min_samples: int
) -> NeighborModel | None:
    """Embed ``samples`` into a nearest-neighbour index, or ``None`` on a cold start.

    Cold start — too little to compare against — is fewer than ``min_samples`` usable
    rows or fewer than :data:`MIN_DISTINCT_CATEGORIES` distinct categories; blank-text
    rows are dropped first (nothing to embed). The caller treats ``None`` as "no
    suggestions", leaving rows in the queue, identical to the classifier's contract.
    """
    usable = [s for s in samples if s.text.strip()]
    if len(usable) < max(min_samples, MIN_DISTINCT_CATEGORIES):
        return None
    if len({s.category_id for s in usable}) < MIN_DISTINCT_CATEGORIES:
        return None

    texts = [s.text for s in usable]
    matrix = embedder.encode(texts)

    return NeighborModel(matrix=matrix, labels=[s.category_id for s in usable], texts=texts)


@lru_cache(maxsize=4)
def load_embedder(model: str, revision: str | None = None) -> Embedder:
    """A sentence-transformers embedder for ``model`` at ``revision``, cached per
    process (the load is the expensive part — the weights are bundled in the image,
    see the Dockerfile).

    Imported lazily: sentence-transformers pulls torch (hundreds of MB), so a process
    that never reaches the review queue never imports it. Encoding L2-normalizes, so
    :class:`NeighborModel` can treat a dot product as the cosine similarity.

    With ``HF_HUB_OFFLINE=1`` set at runtime (the Pi never touches the network), a
    missing model raises here rather than triggering a download — the DB layer
    catches that and falls back to no suggestions. ``revision`` must match what the
    build bundled: a commit-hash download leaves no ``main`` ref, so the offline load
    has to request the same revision it cached.
    """
    from sentence_transformers import SentenceTransformer

    transformer = SentenceTransformer(model, revision=revision)

    return _SentenceTransformerEmbedder(transformer)


class _SentenceTransformerEmbedder:
    """Adapts a loaded :class:`~sentence_transformers.SentenceTransformer` to the
    :class:`Embedder` protocol (encode → L2-normalized ``float32`` matrix)."""

    def __init__(self, transformer: object) -> None:
        self._transformer = transformer

    def encode(self, texts: Sequence[str]) -> NDArray[np.float32]:
        vectors = self._transformer.encode(  # type: ignore[attr-defined]
            list(texts), normalize_embeddings=True, convert_to_numpy=True
        )

        return np.asarray(vectors, dtype=np.float32)
