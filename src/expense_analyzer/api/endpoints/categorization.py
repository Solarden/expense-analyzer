"""Review queue: categorization layer 2 — the classifier + manual queue (design §7.7).

The queue lists the still-uncategorized transactions with the classifier's
suggestion next to each (its top category + confidence). "Train & classify now"
trains on your confirmed categorizations and auto-applies the confident
predictions; the rest stay here for a human to tag. Tagging a row marks it
``manual``, which feeds the next training run — the active-learning loop.

Handlers stay thin — all DB access goes through
:mod:`expense_analyzer.queries.classifier` (and ``set_category`` for the manual
verdict, shared with the transactions page so a queue tag behaves identically).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from expense_analyzer.api.categorize import apply_categorization
from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import CategorizeForm
from expense_analyzer.auth import require_user
from expense_analyzer.config import get_settings
from expense_analyzer.models import Scope
from expense_analyzer.queries import categories as category_queries
from expense_analyzer.queries import classifier as classifier_queries
from expense_analyzer.queries import embeddings as embeddings_queries
from expense_analyzer.templating import templates

router = APIRouter(
    prefix="/dashboard/queue", tags=["categorization"], dependencies=[Depends(require_user)]
)


def _classify_flash(categorized: int | None, queued: int | None, trained: int | None) -> str | None:
    """The flash for a classify run carried across the redirect, or ``None`` on a
    plain page load. ``trained=0`` is the cold start (not enough labeled data)."""
    if categorized is None:
        return None
    if trained == 0:
        return (
            "Not enough categorized transactions to train the classifier yet — "
            "tag a few by hand and try again."
        )

    parts = [f"{categorized} categorized"]
    if queued:
        parts.append(f"{queued} left for review")

    return "Classified — " + ", ".join(parts) + "."


@router.get("", response_class=HTMLResponse)
def queue_page(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    page: str | None = None,
    categorized: int | None = None,
    queued: int | None = None,
    trained: int | None = None,
) -> HTMLResponse:
    page_num = max(1, int(page)) if page and page.isdigit() else 1
    result = classifier_queries.review_queue(
        session, page=page_num, page_size=get_settings().page_size
    )

    # Layer 3 (Phase 12): the nearest already-categorized transaction for each
    # queued row, keyed by tx id. A suggestion only — fail-safe to {} (cold start,
    # disabled, or model unavailable), so the queue renders either way.
    neighbors = embeddings_queries.neighbor_suggestions(
        session, [r.transaction for r in result.rows]
    )

    categories = category_queries.list_categories(session)
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "user": user,
            "page": result,
            "neighbors": neighbors,
            "categories": categories,
            "category_names": {c.id: c.name for c in categories if c.id is not None},
            "scopes": [s.value for s in Scope],
            "flash": _classify_flash(categorized, queued, trained),
        },
    )


@router.post("/classify")
def classify(session: DbSession) -> RedirectResponse:
    result = classifier_queries.classify(session)

    return RedirectResponse(
        f"/dashboard/queue?categorized={result.categorized}&queued={result.queued}"
        f"&trained={int(result.trained)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{tx_id}/categorize")
def categorize(
    tx_id: int,
    form: Annotated[CategorizeForm, Form()],
    session: DbSession,
    page: str | None = None,
) -> RedirectResponse:
    """Tag a queued transaction by hand (``source = manual``) and return to the
    queue. Accepting the classifier's suggestion is just submitting it pre-selected
    — a human verdict either way, which is what feeds the next training run."""
    apply_categorization(session, tx_id=tx_id, raw_category_id=form.category_id, scope=form.scope)

    page_num = max(1, int(page)) if page and page.isdigit() else 1
    dest = "/dashboard/queue" if page_num == 1 else f"/dashboard/queue?page={page_num}"

    return RedirectResponse(dest, status_code=status.HTTP_303_SEE_OTHER)
