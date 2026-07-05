"""LLM categorization (piec Ollama) — primary categorizer, classifier fallback.

The owner's decision (PR 2): the LLM on piec is the *primary* categorizer for the
review-queue "classify now" action; the local classifier (layer 2) is the
fallback used only when piec is unreachable. Import stays rules-only — the LLM
never runs inline on upload.

Precedence and eligibility mirror the classifier exactly:

- Candidates are the untouched uncategorized rows (``category_id IS NULL`` and
  ``source = import_csv``) — the same set :func:`classifier.classify` takes. The
  candidate filter is reused from the classifier so the two can't drift.
- A confident verdict (``>= llm_confidence_threshold``) sets ``category_id``,
  ``source = llm`` and ``confidence``. Below the threshold the row stays in the
  queue. An out-of-range category id (a model hallucination) is skipped, not
  applied.
- If piec is unreachable/slow/garbled (:class:`OllamaError`), the run falls back
  to :func:`classifier.classify`; rows tagged before the failure are kept.
"""

import logging

from sqlmodel import Session, col, select

from expense_analyzer.config import Settings, get_settings
from expense_analyzer.models import Category, CategoryKind, Transaction, TxSource
from expense_analyzer.ollama import OllamaClient, OllamaError
from expense_analyzer.queries.categorize.classifier import (
    ClassifyResult,
    _candidate_filter,
    classify,
)

log = logging.getLogger("expense_analyzer.categorize")

# Expense/income only — transfers are handled by transfer linking (same as the classifier).
_LEARNABLE_KINDS = (CategoryKind.expense, CategoryKind.income)


def _learnable_categories(session: Session) -> list[Category]:
    """Expense/income categories — the LLM's candidate labels."""
    query = select(Category).where(col(Category.kind).in_(_LEARNABLE_KINDS))

    return list(session.exec(query).all())


def classify_llm(
    session: Session, *, settings: Settings | None = None, client: OllamaClient | None = None
) -> ClassifyResult:
    """Categorize uncategorized rows via piec's Ollama, one call per row.

    Raises :class:`OllamaError` if piec fails on any row (the caller falls back to
    the local classifier); rows applied before the failure are committed first, so
    the fallback only reconsiders what's left.
    """
    settings = settings or get_settings()
    candidates = list(session.exec(_candidate_filter(select(Transaction))).all())
    categories = _learnable_categories(session)
    if not candidates or not categories:
        return ClassifyResult(
            categorized=0, queued=len(candidates), candidates=len(candidates), trained=True
        )

    client = client or OllamaClient.from_settings(settings)
    valid_ids = {c.id for c in categories}
    cat_pairs = [(c.id, c.name) for c in categories if c.id is not None]
    threshold = settings.llm_confidence_threshold

    categorized = 0
    queued = 0
    try:
        for tx in candidates:
            verdict = client.categorize(
                merchant=tx.merchant_normalized,
                description=tx.raw_description,
                amount=tx.amount,
                categories=cat_pairs,
            )
            if verdict.category_id not in valid_ids or verdict.confidence < threshold:
                queued += 1
                continue
            tx.category_id = verdict.category_id
            tx.source = TxSource.llm
            tx.confidence = verdict.confidence
            session.add(tx)
            categorized += 1
    except OllamaError:
        session.commit()  # keep whatever was tagged before piec went away
        raise

    session.commit()

    return ClassifyResult(
        categorized=categorized, queued=queued, candidates=len(candidates), trained=True
    )


def categorize_uncategorized(
    session: Session, *, settings: Settings | None = None, client: OllamaClient | None = None
) -> ClassifyResult:
    """The "classify now" entry point: LLM (piec) primary, classifier fallback.

    With the LLM enabled and configured, try piec first; on :class:`OllamaError`
    (unreachable/slow/garbled) log a warning and fall back to the local classifier.
    With the LLM off, this is just the classifier — the pre-PR-2 behaviour.
    """
    settings = settings or get_settings()
    if settings.llm_enabled and settings.llm_base_url:
        try:
            return classify_llm(session, settings=settings, client=client)
        except OllamaError:
            # A silent fallback would hide a down piec — the owner would think the
            # LLM is working when it never runs. Say so, then use the local model.
            log.warning("LLM categorizer unreachable; falling back to the local classifier")

    return classify(session, settings=settings)
