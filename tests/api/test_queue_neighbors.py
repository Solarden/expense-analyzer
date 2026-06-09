"""Embeddings neighbours (layer 3): query layer + the review-queue render
(Phase 12, design §7.7 point 3).

The query-layer tests inject a deterministic fake :class:`Embedder` (token overlap,
no torch) plus a permissive :class:`Settings`, so a handful of fixture rows is
enough — the real model is never loaded. The render test goes through the live
endpoint, where the real loader is unavailable in the suite: that exercises the
fail-safe path (no model → no suggestions → the queue still renders 200)."""

from collections.abc import Callable, Sequence

import numpy as np
from fastapi import status
from fastapi.testclient import TestClient
from sqlmodel import Session

from expense_analyzer.config import Settings
from expense_analyzer.models import Account, Category, Transaction, TxSource
from expense_analyzer.queries.categorize import embeddings as eq

# Permissive: a tiny fixture set clears the floor; the floor on similarity stays
# meaningful so the "too far" case can be exercised.
# embeddings_enabled is set explicitly: the suite disables it via env (see
# conftest), and an injected Settings still reads that env for unset fields.
_LOW = Settings(
    embeddings_enabled=True,
    embeddings_min_training_samples=4,
    embeddings_neighbors=3,
    embeddings_min_similarity=0.45,
)


class FakeEmbedder:
    """Bag-of-known-words → L2-normalized vector, so cosine similarity is token
    overlap. No model, fully deterministic (see test_embeddings.py)."""

    def __init__(self, vocab: dict[str, int]) -> None:
        self._vocab = vocab

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), len(self._vocab)), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in text.lower().split():
                if word in self._vocab:
                    matrix[i, self._vocab[word]] += 1.0
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0

        return matrix / norms


def _embedder() -> FakeEmbedder:
    return FakeEmbedder({"biedronka": 0, "netflix": 1})


def _seed_labels(
    make_transaction: Callable[..., Transaction],
    *,
    account_id: int,
    food_id: int,
    fun_id: int,
    n_each: int = 4,
    source: TxSource = TxSource.manual,
) -> None:
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


def test_cold_start_returns_no_suggestions(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
) -> None:
    tx = make_transaction(account_id=account.id, amount=-5000, merchant_normalized="BIEDRONKA 12")

    assert eq.neighbor_suggestions(db_session, [tx], settings=_LOW, embedder=_embedder()) == {}


def test_disabled_returns_no_suggestions_without_touching_the_embedder(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    _seed_labels(make_transaction, account_id=account.id, food_id=food.id, fun_id=fun.id)
    tx = make_transaction(account_id=account.id, amount=-5000, merchant_normalized="BIEDRONKA 12")

    class Boom:
        def encode(self, texts):  # pragma: no cover - must never be called
            raise AssertionError("embedder used while disabled")

    off = Settings(embeddings_enabled=False, embeddings_min_training_samples=4)
    assert eq.neighbor_suggestions(db_session, [tx], settings=off, embedder=Boom()) == {}


def test_suggests_nearest_neighbour_for_a_candidate(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    _seed_labels(make_transaction, account_id=account.id, food_id=food.id, fun_id=fun.id)
    tx = make_transaction(account_id=account.id, amount=-5000, merchant_normalized="BIEDRONKA 12")

    result = eq.neighbor_suggestions(db_session, [tx], settings=_LOW, embedder=_embedder())

    assert tx.id in result
    suggestion = result[tx.id]
    assert suggestion.category_id == food.id
    assert suggestion.example_text == "BIEDRONKA"
    assert 0.45 <= suggestion.similarity <= 1.0


def test_far_candidate_is_absent(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    _seed_labels(make_transaction, account_id=account.id, food_id=food.id, fun_id=fun.id)
    far = make_transaction(
        account_id=account.id, amount=-5000, merchant_normalized="MYSTERY VENDOR"
    )

    result = eq.neighbor_suggestions(db_session, [far], settings=_LOW, embedder=_embedder())

    assert far.id not in result


def test_blank_text_candidate_is_skipped(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    _seed_labels(make_transaction, account_id=account.id, food_id=food.id, fun_id=fun.id)
    # No merchant and an empty raw description -> nothing to embed.
    blank = make_transaction(
        account_id=account.id, amount=-5000, merchant_normalized=None, raw_description=""
    )

    result = eq.neighbor_suggestions(db_session, [blank], settings=_LOW, embedder=_embedder())

    assert result == {}


def test_does_not_use_the_classifiers_own_output_as_a_neighbour(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    # Only classifier-sourced labels exist -> not "confirmed" -> cold start, no
    # suggestions (the shared confirmed-label selection excludes machine output).
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    _seed_labels(
        make_transaction,
        account_id=account.id,
        food_id=food.id,
        fun_id=fun.id,
        source=TxSource.classifier,
    )
    tx = make_transaction(account_id=account.id, amount=-5000, merchant_normalized="BIEDRONKA 12")

    assert eq.neighbor_suggestions(db_session, [tx], settings=_LOW, embedder=_embedder()) == {}


# --- endpoint (fail-safe render) ------------------------------------------


def test_queue_page_renders_with_similar_column_when_layer3_yields_nothing(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
) -> None:
    # Layer 3 is disabled in the suite (no heavy model load), so neighbour
    # suggestions fail safe to none — the page must still render and show the new
    # column. This is the same render path a real cold start / unavailable model hits.
    make_transaction(account_id=account.id, amount=-5000, merchant_normalized="BIEDRONKA 12")

    response = auth_client.get("/dashboard/queue")

    assert response.status_code == status.HTTP_200_OK
    assert "Similar to" in response.text
