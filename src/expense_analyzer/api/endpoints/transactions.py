"""Transactions page: filtered/paginated list and manual categorization (design §8)."""

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import CategorizeForm
from expense_analyzer.auth import require_user
from expense_analyzer.config import get_settings
from expense_analyzer.models import Scope
from expense_analyzer.queries import accounts, categories, stats, transactions
from expense_analyzer.queries.transactions import UNCATEGORIZED, TransactionFilters
from expense_analyzer.templating import templates

router = APIRouter(
    prefix="/dashboard/transactions", tags=["transactions"], dependencies=[Depends(require_user)]
)


@router.get("", response_class=HTMLResponse)
def list_transactions(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    account_id: int | None = None,
    month: str | None = None,
    category: str | None = None,  # "none" = uncategorized, a digit = that category
    scope: Scope | None = None,
    q: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    uncategorized = category == UNCATEGORIZED
    category_id = int(category) if category and category.isdigit() else None

    filters = TransactionFilters(
        account_id=account_id,
        month=month or None,
        category_id=category_id,
        uncategorized=uncategorized,
        scope=scope,
        search=q or None,
    )
    result = transactions.list_transactions(
        session, filters, page=page, page_size=get_settings().page_size
    )

    def page_query(target_page: int) -> str:
        """Querystring for a pager link — keeps the active filters, swaps page."""
        params: list[tuple[str, str]] = []
        if account_id is not None:
            params.append(("account_id", str(account_id)))
        if month:
            params.append(("month", month))
        if category:
            params.append(("category", category))
        if scope:
            params.append(("scope", scope.value))
        if q:
            params.append(("q", q))
        params.append(("page", str(max(1, target_page))))

        return urlencode(params)

    # Where the categorize form returns to — the current filtered/paged view.
    return_to = "/dashboard/transactions"
    if request.url.query:
        return_to += f"?{request.url.query}"

    return templates.TemplateResponse(
        request,
        "transactions.html",
        {
            "user": user,
            "page": result,
            "accounts": accounts.list_accounts(session),
            "categories": categories.list_categories(session),
            "months": stats.available_months(session),
            "scopes": [s.value for s in Scope],
            "page_query": page_query,
            "return_to": return_to,
            # Echo the active filters back so the form stays sticky and the pager
            # carries them across pages.
            "f_account_id": account_id,
            "f_month": month or "",
            "f_category": category or "",
            "f_scope": scope.value if scope else "",
            "f_q": q or "",
        },
    )


@router.post("/{tx_id}/categorize")
def categorize(
    tx_id: int,
    form: Annotated[CategorizeForm, Form()],
    session: DbSession,
) -> RedirectResponse:
    parsed_category_id: int | None = None
    if form.category_id:
        if not form.category_id.isdigit():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid category id: {form.category_id!r}",
            )
        parsed_category_id = int(form.category_id)
        if categories.get_category(session, parsed_category_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"category {parsed_category_id} not found",
            )

    if (
        transactions.set_category(
            session, tx_id=tx_id, category_id=parsed_category_id, scope=form.scope
        )
        is None
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"transaction {tx_id} not found"
        )

    # Return to the filtered/paged view the user came from. Only accept the list
    # path itself or the list path with a query string — no open redirect, and no
    # sibling path like "/dashboard/transactionsX" (defense in depth, per review).
    list_path = "/dashboard/transactions"
    allowed = form.return_to == list_path or form.return_to.startswith(f"{list_path}?")
    dest = form.return_to if allowed else list_path

    return RedirectResponse(dest, status_code=status.HTTP_303_SEE_OTHER)
