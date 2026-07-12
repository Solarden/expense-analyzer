"""Natural-language query page (PR 4): ``/dashboard/ask``.

A single GET handler behind ``require_user``. A ``<form method="get">`` — a
shareable URL, no CSRF concern, mirroring the transactions filter bar. Empty ``q``
→ just the box; non-empty → :func:`answer` and render. The LLM never emits SQL: it
produces a validated filter and the app answers from its own data (see
:mod:`expense_analyzer.queries.money.nl_query`).
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from expense_analyzer.api.deps import CurrentLens, CurrentUser, DbSession
from expense_analyzer.auth import require_user
from expense_analyzer.config import get_settings
from expense_analyzer.queries.categorize import categories
from expense_analyzer.queries.money.nl_query import answer
from expense_analyzer.templating import templates

router = APIRouter(prefix="/dashboard", tags=["query"], dependencies=[Depends(require_user)])


@router.get("/ask", response_class=HTMLResponse)
def ask(
    request: Request,
    user: CurrentUser,
    lens: CurrentLens,
    session: DbSession,
    q: str | None = None,
) -> HTMLResponse:
    question = (q or "").strip()
    result = answer(session, question, viewer_id=user.id, lens=lens) if question else None
    # Names for the read-only row list (templates read a map, not tx.category — the
    # same pattern the transactions/overview pages use, so no lazy relationship load).
    category_names = {c.id: c.name for c in categories.list_categories(session) if c.id is not None}

    return templates.TemplateResponse(
        request,
        "money/query.html",
        {
            "user": user,
            "q": q or "",
            "result": result,
            "category_names": category_names,
            "llm_enabled": get_settings().llm_enabled,
        },
    )
