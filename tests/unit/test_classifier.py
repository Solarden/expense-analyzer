"""Pure-logic tests for the categorization classifier (Phase 11, layer 2).

No DB, no session — just the TF-IDF + logistic-regression model and its
cold-start guards (mirrors test_rules.py for the deterministic layer)."""

from expense_analyzer.classifier import (
    MIN_DISTINCT_CATEGORIES,
    TrainingSample,
    build_text,
    train,
)

FOOD = 1
FUN = 2


def _labeled(n_each: int) -> list[TrainingSample]:
    """A clean two-class training set: ``n_each`` BIEDRONKA->Food, ``n_each`` NETFLIX->Fun."""
    return [TrainingSample("BIEDRONKA", FOOD) for _ in range(n_each)] + [
        TrainingSample("NETFLIX", FUN) for _ in range(n_each)
    ]


def test_build_text_prefers_merchant_then_falls_back_to_raw():
    assert build_text("BIEDRONKA", "card payment 123") == "BIEDRONKA"
    assert build_text(None, "ATM CASH") == "ATM CASH"
    assert build_text("", "raw only") == "raw only"  # blank merchant -> raw
    assert build_text(None, "") == ""


def test_train_returns_none_below_min_samples():
    # Two classes present, but fewer rows than required -> cold start.
    assert train(_labeled(2), min_samples=25) is None


def test_train_returns_none_with_single_category():
    # Enough rows, but only one class -> nothing to discriminate.
    samples = [TrainingSample("BIEDRONKA", FOOD) for _ in range(10)]
    assert train(samples, min_samples=4) is None


def test_train_drops_blank_text_rows():
    # The 6 blank rows carry no signal and are dropped, leaving 2 usable < min 5.
    samples = [TrainingSample("   ", FOOD) for _ in range(6)] + [
        TrainingSample("BIEDRONKA", FOOD),
        TrainingSample("NETFLIX", FUN),
    ]
    assert train(samples, min_samples=5) is None


def test_min_distinct_categories_is_two():
    assert MIN_DISTINCT_CATEGORIES == 2


def test_trained_model_predicts_the_right_class_with_confidence():
    model = train(_labeled(6), min_samples=4)
    assert model is not None

    grocery = model.predict("BIEDRONKA SP ZOO WARSZAWA")
    assert grocery is not None
    assert grocery.category_id == FOOD
    assert 0.5 < grocery.confidence <= 1.0

    stream = model.predict("NETFLIX.COM")
    assert stream is not None
    assert stream.category_id == FUN


def test_predict_returns_none_for_empty_text():
    model = train(_labeled(6), min_samples=4)
    assert model is not None
    assert model.predict("") is None
    assert model.predict("   ") is None
