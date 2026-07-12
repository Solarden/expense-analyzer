"""Transactions page: filtered/paginated list, inline categorize, and the
single-row edit layer — manual (cash) entry, notes, edit and delete (Phase 13)."""

from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from expense_analyzer.api.categorize import apply_categorization, parse_category_id
from expense_analyzer.api.deps import CurrentLens, CurrentUser, DbSession
from expense_analyzer.api.forms import (
    CategorizeForm,
    EditTransactionForm,
    ManualTransactionForm,
    NoteForm,
    TxDirection,
)
from expense_analyzer.auth import require_user
from expense_analyzer.clock import local_today
from expense_analyzer.config import get_settings
from expense_analyzer.models import Lens, Owner, Scope, Transaction
from expense_analyzer.money import MoneyParseError, from_minor_units, parse_pln
from expense_analyzer.queries.categorize import categories
from expense_analyzer.queries.core import accounts
from expense_analyzer.queries.money import stats, transactions
from expense_analyzer.queries.money.transactions import UNCATEGORIZED, TransactionFilters
from expense_analyzer.templating import templates

router = APIRouter(
    prefix="/dashboard/transactions", tags=["transactions"], dependencies=[Depends(require_user)]
)

_LIST_PATH = "/dashboard/transactions"
# Selectable rows-per-page. A whitelist (not a raw int) so a hand-edited ?size=
# can't ask for a 100k-row page on the Pi. EA_PAGE_SIZE stays the default when no
# (or an off-list) size is chosen.
_PAGE_SIZES = (25, 50, 100, 200)


def _safe_return_to(candidate: str) -> str:
    """Guard a return-to path against open redirects: accept only the list itself
    or the list with a query string (no sibling like ``/dashboard/transactionsX``).
    """
    if candidate == _LIST_PATH or candidate.startswith(f"{_LIST_PATH}?"):
        return candidate

    return _LIST_PATH


def _list_context(
    request: Request,
    user: Owner,
    session: Session,
    *,
    lens: Lens = Lens.all,
    account_id: str | None = None,
    month: str | None = None,
    category: str | None = None,
    q: str | None = None,
    page: str | None = None,
    size: str | None = None,
    error: str | None = None,
) -> dict:
    # The filter bar auto-submits every control on change, so the "— all … —"
    # options arrive as empty strings (and a hand-edited URL may carry garbage).
    # Parse each leniently into None rather than declaring typed params that 422
    # on an empty/invalid value (consistent with the malformed-month fix, Phase 4).
    uncategorized = category == UNCATEGORIZED
    category_id = int(category) if category and category.isdigit() else None
    parsed_account_id = int(account_id) if account_id and account_id.isdigit() else None
    page_num = max(1, int(page)) if page and page.isdigit() else 1
    # Explicit choice only if it's on the whitelist; otherwise fall back to the
    # configured default (and don't echo a bogus value into the pager links).
    parsed_size = int(size) if size and size.isdigit() and int(size) in _PAGE_SIZES else None
    resolved_size = parsed_size if parsed_size is not None else get_settings().page_size

    filters = TransactionFilters(
        account_id=parsed_account_id,
        month=month or None,
        category_id=category_id,
        uncategorized=uncategorized,
        search=q or None,
    )
    result = transactions.list_transactions(
        session, filters, page=page_num, page_size=resolved_size, viewer_id=user.id, lens=lens
    )

    def page_query(target_page: int) -> str:
        """Querystring for a pager link — keeps the active filters, swaps page."""
        params: list[tuple[str, str]] = []
        if parsed_account_id is not None:
            params.append(("account_id", str(parsed_account_id)))
        if month:
            params.append(("month", month))
        if category:
            params.append(("category", category))
        if q:
            params.append(("q", q))
        if parsed_size is not None:
            params.append(("size", str(parsed_size)))
        params.append(("page", str(max(1, target_page))))

        return urlencode(params)

    # Where the categorize / edit forms return to — the current filtered/paged view.
    return_to = _LIST_PATH
    if request.url.query and request.method == "GET":
        return_to += f"?{request.url.query}"

    all_categories = categories.list_categories(session)
    # Per-row data for the inline edit modal (mirrors _edit_context, precomputed
    # here so the template stays declarative): which rows are hand-entered (fully
    # editable), and the unsigned magnitude + direction the amount field needs.
    manual_batch = transactions.manual_batch_id(session)  # one query, not one per row
    edit_meta = {
        tx.id: {
            "is_manual": tx.import_batch_id == manual_batch,
            "magnitude": str(from_minor_units(abs(tx.amount))),
            "direction": (
                TxDirection.income.value if tx.amount >= 0 else TxDirection.expense.value
            ),
        }
        for tx in result.rows
    }
    return {
        "user": user,
        "page": result,
        "accounts": accounts.list_accounts(session),
        "categories": all_categories,
        "category_colors": {c.id: c.color for c in all_categories if c.id is not None},
        "edit_meta": edit_meta,
        "months": stats.available_months(session, viewer_id=user.id, lens=lens),
        "scopes": [s.value for s in Scope],
        "directions": [d.value for d in TxDirection],
        "page_sizes": list(_PAGE_SIZES),
        "today": local_today().isoformat(),
        "page_query": page_query,
        "return_to": return_to,
        "error": error,
        # Echo the active filters back so the form stays sticky and the pager
        # carries them across pages.
        "f_account_id": parsed_account_id,
        "f_month": month or "",
        "f_category": category or "",
        "f_q": q or "",
        "f_page_size": resolved_size,
    }


@router.get("", response_class=HTMLResponse)
def list_transactions(
    request: Request,
    user: CurrentUser,
    lens: CurrentLens,
    session: DbSession,
    account_id: str | None = None,  # "" (all accounts) or a digit
    month: str | None = None,
    category: str | None = None,  # "none" = uncategorized, a digit = that category
    q: str | None = None,
    page: str | None = None,
    size: str | None = None,  # rows per page; off-list -> EA_PAGE_SIZE default
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "money/transactions.html",
        _list_context(
            request,
            user,
            session,
            lens=lens,
            account_id=account_id,
            month=month,
            category=category,
            q=q,
            page=page,
            size=size,
        ),
    )


@router.post("/{tx_id}/categorize")
def categorize(
    tx_id: int,
    form: Annotated[CategorizeForm, Form()],
    user: CurrentUser,
    session: DbSession,
) -> RedirectResponse:
    apply_categorization(
        session,
        tx_id=tx_id,
        raw_category_id=form.category_id,
        scope=form.scope,
        viewer_id=user.id,
    )

    return RedirectResponse(_safe_return_to(form.return_to), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{tx_id}/note")
def update_note(
    tx_id: int,
    form: Annotated[NoteForm, Form()],
    user: CurrentUser,
    session: DbSession,
) -> RedirectResponse:
    """Save the note from the note modal (works on any row, imported or manual)."""
    result = transactions.set_note(
        session, tx_id=tx_id, note=form.note.strip() or None, viewer_id=user.id
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found")

    return RedirectResponse(_safe_return_to(form.return_to), status_code=status.HTTP_303_SEE_OTHER)


def _signed_amount(magnitude_text: str, direction: TxDirection) -> int:
    """Parse a positive PLN magnitude and sign it by direction. Raises
    :class:`MoneyParseError` on unparseable text or a non-positive amount."""
    magnitude = abs(parse_pln(magnitude_text))
    if magnitude == 0:
        raise MoneyParseError("amount must be greater than zero")

    return -magnitude if direction is TxDirection.expense else magnitude


@router.post("/add", response_class=HTMLResponse)
def add_transaction(
    request: Request,
    form: Annotated[ManualTransactionForm, Form()],
    user: CurrentUser,
    lens: CurrentLens,
    session: DbSession,
) -> Response:
    """Hand-enter a transaction (mainly cash — the only entry path for a cash
    account). Bad input re-renders the list with a red flash, not a 500."""
    error: str | None = None
    if accounts.get_account(session, form.account_id) is None:
        error = "Pick an account."
    elif not form.description.strip():
        error = "Description is required."
    else:
        try:
            amount = _signed_amount(form.amount, form.direction)
        except MoneyParseError as exc:
            error = f"Could not read the amount: {exc}"

    if error is not None:
        return templates.TemplateResponse(
            request,
            "money/transactions.html",
            _list_context(request, user, session, lens=lens, error=error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    transactions.create_manual_transaction(
        session,
        account_id=form.account_id,
        booked_date=form.booked_date,
        amount=amount,
        description=form.description.strip(),
        category_id=parse_category_id(session, form.category_id),
        scope=form.scope,
        note=form.note.strip() or None,
        owner_id=user.id,
    )

    return RedirectResponse(_safe_return_to(form.return_to), status_code=status.HTTP_303_SEE_OTHER)


def _edit_context(
    request: Request,
    user: Owner,
    session: Session,
    tx: Transaction,
    *,
    return_to: str,
    error: str | None = None,
) -> dict:
    return {
        "user": user,
        "tx": tx,
        "is_manual": transactions.is_manual_entry(session, tx),
        "accounts": accounts.list_accounts(session),
        "categories": categories.list_categories(session),
        "scopes": [s.value for s in Scope],
        "directions": [d.value for d in TxDirection],
        "amount_magnitude": str(from_minor_units(abs(tx.amount))),
        "direction": (TxDirection.income.value if tx.amount >= 0 else TxDirection.expense.value),
        "return_to": _safe_return_to(return_to),
        "error": error,
    }


@router.get("/{tx_id}/edit", response_class=HTMLResponse)
def edit_transaction_form(
    request: Request,
    tx_id: int,
    user: CurrentUser,
    session: DbSession,
    return_to: str = _LIST_PATH,
) -> HTMLResponse:
    tx = transactions.get_transaction(session, tx_id, viewer_id=user.id)
    if tx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found")

    return templates.TemplateResponse(
        request,
        "money/transaction_edit.html",
        _edit_context(request, user, session, tx, return_to=return_to),
    )


@router.post("/{tx_id}/edit", response_class=HTMLResponse)
def edit_transaction(
    request: Request,
    tx_id: int,
    form: Annotated[EditTransactionForm, Form()],
    user: CurrentUser,
    session: DbSession,
) -> Response:
    tx = transactions.get_transaction(session, tx_id, viewer_id=user.id)
    if tx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found")

    category_id = parse_category_id(session, form.category_id)
    note = form.note.strip() or None

    # Category/scope/note apply to every row. The money fields are rewritten only
    # for manual entries — an imported row's amount/date/description are the bank's
    # source of truth (and feed its import fingerprint), so they stay read-only.
    money_fields: dict = {}
    if transactions.is_manual_entry(session, tx):
        if form.account_id is None or accounts.get_account(session, form.account_id) is None:
            error = "Pick an account."
        elif not form.description.strip():
            error = "Description is required."
        else:
            try:
                amount = _signed_amount(form.amount, form.direction)
            except MoneyParseError as exc:
                error = f"Could not read the amount: {exc}"
            else:
                error = None
                money_fields = {
                    "account_id": form.account_id,
                    "booked_date": form.booked_date,
                    "amount": amount,
                    "description": form.description.strip(),
                }
        if error is not None:
            return templates.TemplateResponse(
                request,
                "money/transaction_edit.html",
                _edit_context(request, user, session, tx, return_to=form.return_to, error=error),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    transactions.update_transaction(
        session,
        tx_id=tx_id,
        viewer_id=user.id,
        category_id=category_id,
        scope=form.scope,
        note=note,
        **money_fields,
    )

    return RedirectResponse(_safe_return_to(form.return_to), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{tx_id}/delete")
def delete_transaction(
    tx_id: int,
    user: CurrentUser,
    session: DbSession,
    return_to: Annotated[str, Form()] = _LIST_PATH,
) -> RedirectResponse:
    """Soft-delete a manual entry. Imported rows are removed by rolling back their
    import batch, not one at a time — so deletion is gated to manual entries."""
    tx = transactions.get_transaction(session, tx_id, viewer_id=user.id)
    if tx is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="transaction not found")
    if not transactions.is_manual_entry(session, tx):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only manual entries can be deleted; roll back the import batch instead",
        )
    transactions.soft_delete_transaction(session, tx_id=tx_id, viewer_id=user.id)

    return RedirectResponse(_safe_return_to(return_to), status_code=status.HTTP_303_SEE_OTHER)
