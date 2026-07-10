"""LLM categorization (PR 2): query layer + the "classify now" orchestrator.

The Ollama client is stubbed (no network): a fake returns a fixed verdict, or
raises :class:`OllamaError` to simulate a down Ollama host. The real HTTP client is
covered in ``tests/unit/test_ollama.py``.
"""

from collections.abc import Callable

from sqlmodel import Session

from expense_analyzer.config import Settings
from expense_analyzer.models import Account, Category, Transaction, TxSource
from expense_analyzer.ollama import LlmVerdict, OllamaError
from expense_analyzer.queries.categorize import llm as lq


class _FakeClient:
    """Stands in for OllamaClient: returns a fixed verdict, or raises to simulate a
    down Ollama host. Records how many times it was asked, so a test can assert the LLM
    path was (or wasn't) taken."""

    def __init__(self, verdict: LlmVerdict | None = None, *, error: bool = False) -> None:
        self._verdict = verdict
        self._error = error
        self.calls = 0

    def categorize(self, **_kw: object) -> LlmVerdict:
        self.calls += 1
        if self._error:
            raise OllamaError("Ollama host down")
        assert self._verdict is not None
        return self._verdict


# LLM on and configured; classifier floor/threshold low so the fallback tests can
# train on a handful of fixture labels.
_LLM = Settings(llm_enabled=True, llm_base_url="http://ollama:11434", llm_confidence_threshold=0.7)
_LLM_LOW_FALLBACK = Settings(
    llm_enabled=True,
    llm_base_url="http://ollama:11434",
    classifier_min_training_samples=4,
    classifier_confidence_threshold=0.5,
)


def _uncategorized(make_transaction: Callable[..., Transaction], *, account_id: int) -> Transaction:
    return make_transaction(
        account_id=account_id,
        amount=-5000,
        merchant_normalized="BIEDRONKA",
        source=TxSource.import_csv,
    )


def _seed_labels(
    make_transaction: Callable[..., Transaction], *, account_id: int, food_id: int, fun_id: int
) -> None:
    """Enough confirmed labels for the classifier to train (BIEDRONKA->Food, NETFLIX->Fun)."""
    for _ in range(6):
        make_transaction(
            account_id=account_id,
            amount=-5000,
            merchant_normalized="BIEDRONKA",
            category_id=food_id,
            source=TxSource.manual,
        )
        make_transaction(
            account_id=account_id,
            amount=-3000,
            merchant_normalized="NETFLIX",
            category_id=fun_id,
            source=TxSource.manual,
        )


# --- classify_llm ----------------------------------------------------------


def test_high_confidence_verdict_auto_applies_with_llm_source(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    tx = _uncategorized(make_transaction, account_id=account.id)
    client = _FakeClient(LlmVerdict(category_id=food.id, confidence=0.9))

    result = lq.classify_llm(db_session, settings=_LLM, client=client)

    db_session.refresh(tx)
    assert result.categorized == 1
    assert tx.category_id == food.id
    assert tx.source == TxSource.llm
    assert tx.confidence == 0.9


def test_low_confidence_verdict_stays_in_the_queue(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    tx = _uncategorized(make_transaction, account_id=account.id)
    client = _FakeClient(LlmVerdict(category_id=food.id, confidence=0.3))

    result = lq.classify_llm(db_session, settings=_LLM, client=client)

    db_session.refresh(tx)
    assert result.categorized == 0
    assert result.queued == 1
    assert tx.category_id is None
    assert tx.source == TxSource.import_csv


def test_hallucinated_category_id_is_skipped(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    make_category(name="Food")
    tx = _uncategorized(make_transaction, account_id=account.id)
    client = _FakeClient(LlmVerdict(category_id=9999, confidence=0.99))  # no such category

    result = lq.classify_llm(db_session, settings=_LLM, client=client)

    db_session.refresh(tx)
    assert result.queued == 1
    assert tx.category_id is None
    assert tx.source == TxSource.import_csv


# --- categorize_uncategorized (classify now): LLM primary, classifier fallback ---


def test_falls_back_to_classifier_when_llm_is_down(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    _seed_labels(make_transaction, account_id=account.id, food_id=food.id, fun_id=fun.id)
    tx = _uncategorized(make_transaction, account_id=account.id)
    down = _FakeClient(error=True)

    result = lq.categorize_uncategorized(db_session, settings=_LLM_LOW_FALLBACK, client=down)

    db_session.refresh(tx)
    assert down.calls == 1  # tried the Ollama host once, then bailed to the local classifier
    assert result.categorized == 1
    assert tx.category_id == food.id
    assert tx.source == TxSource.classifier  # the fallback tagged it, not the LLM


def test_llm_disabled_skips_the_client_entirely(
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    make_transaction: Callable[..., Transaction],
) -> None:
    food = make_category(name="Food")
    fun = make_category(name="Fun")
    _seed_labels(make_transaction, account_id=account.id, food_id=food.id, fun_id=fun.id)
    tx = _uncategorized(make_transaction, account_id=account.id)
    # Would raise if consulted — proves the LLM path is skipped when disabled.
    client = _FakeClient(error=True)
    settings = Settings(
        llm_enabled=False, classifier_min_training_samples=4, classifier_confidence_threshold=0.5
    )

    result = lq.categorize_uncategorized(db_session, settings=settings, client=client)

    db_session.refresh(tx)
    assert client.calls == 0
    assert result.categorized == 1
    assert tx.source == TxSource.classifier
