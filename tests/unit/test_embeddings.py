"""Pure-logic tests for the embeddings neighbours (Phase 12, layer 3).

No DB, no session, and crucially **no torch / no model download** — the
:class:`Embedder` protocol is injected as a tiny deterministic fake (a bag-of-known-
words → normalized vector, so cosine similarity is token overlap). That exercises
the kNN vote, the similarity floor and the cold-start guards without the heavy
sentence-transformers stack (mirrors test_classifier.py for layer 2)."""

from collections.abc import Sequence

import numpy as np

from expense_analyzer.embeddings import (
    MIN_DISTINCT_CATEGORIES,
    TrainingSample,
    build,
    build_text,
)

FOOD = 1
FUN = 2


class FakeEmbedder:
    """Maps each known word to a one-hot dimension; a text becomes the L2-normalized
    sum of its words' vectors. Cosine similarity is then token overlap — enough to
    drive the kNN logic deterministically, with no model."""

    def __init__(self, vocab: dict[str, int]) -> None:
        self._vocab = vocab

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), len(self._vocab)), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in text.lower().split():
                if word in self._vocab:
                    matrix[i, self._vocab[word]] += 1.0
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0  # leave all-unknown rows as the zero vector

        return matrix / norms


_VOCAB = {"biedronka": 0, "netflix": 1}


def _embedder() -> FakeEmbedder:
    return FakeEmbedder(_VOCAB)


def _labeled(n_each: int) -> list[TrainingSample]:
    return [TrainingSample("BIEDRONKA", FOOD) for _ in range(n_each)] + [
        TrainingSample("NETFLIX", FUN) for _ in range(n_each)
    ]


def test_build_text_matches_the_other_layers():
    assert build_text("BIEDRONKA", "card payment 123") == "BIEDRONKA"
    assert build_text(None, "ATM CASH") == "ATM CASH"
    assert build_text("", "raw only") == "raw only"
    assert build_text(None, "") == ""


def test_min_distinct_categories_is_two():
    assert MIN_DISTINCT_CATEGORIES == 2


def test_build_returns_none_below_min_samples():
    assert build(_labeled(2), _embedder(), min_samples=25) is None


def test_build_returns_none_with_single_category():
    samples = [TrainingSample("BIEDRONKA", FOOD) for _ in range(10)]
    assert build(samples, _embedder(), min_samples=4) is None


def test_build_drops_blank_text_rows():
    # 6 blank rows carry nothing; only 2 usable remain, below the floor of 5.
    samples = [TrainingSample("   ", FOOD) for _ in range(6)] + [
        TrainingSample("BIEDRONKA", FOOD),
        TrainingSample("NETFLIX", FUN),
    ]
    assert build(samples, _embedder(), min_samples=5) is None


def test_suggests_the_nearest_neighbours_category_with_example():
    model = build(_labeled(6), _embedder(), min_samples=4)
    assert model is not None

    queries = _embedder().encode(["BIEDRONKA 12", "NETFLIX monthly"])
    grocery, stream = model.suggest_batch(queries, k=5, min_similarity=0.45)

    assert grocery is not None
    assert grocery.category_id == FOOD
    assert grocery.example_text == "BIEDRONKA"
    assert 0.45 <= grocery.similarity <= 1.0

    assert stream is not None
    assert stream.category_id == FUN


def test_far_neighbour_below_floor_yields_no_suggestion():
    model = build(_labeled(6), _embedder(), min_samples=4)
    assert model is not None

    # "MYSTERY" shares no known token with anything -> zero vector -> cosine 0.
    [suggestion] = model.suggest_batch(
        _embedder().encode(["MYSTERY VENDOR"]), k=5, min_similarity=0.45
    )
    assert suggestion is None


def test_empty_query_matrix_returns_empty():
    model = build(_labeled(6), _embedder(), min_samples=4)
    assert model is not None
    assert model.suggest_batch(_embedder().encode([]), k=5, min_similarity=0.45) == []


def test_majority_vote_wins_over_a_single_closer_neighbour():
    # Three FUN neighbours vs one FOOD, all equally similar to the query. The vote
    # (3 vs 1) decides, so the suggestion is FUN.
    samples = [
        TrainingSample("shared token", FOOD),
        TrainingSample("shared token", FUN),
        TrainingSample("shared token", FUN),
        TrainingSample("shared token", FUN),
    ]
    embedder = FakeEmbedder({"shared": 0, "token": 1})
    model = build(samples, embedder, min_samples=4)
    assert model is not None

    [suggestion] = model.suggest_batch(embedder.encode(["shared token"]), k=3, min_similarity=0.1)
    assert suggestion is not None
    assert suggestion.category_id == FUN
