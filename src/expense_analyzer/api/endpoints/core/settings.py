"""Setup page (/dashboard/settings): account & category setup and import-batch
rollback. The dashboard landing page (/dashboard) is the overview — see overview.py.

Part of the dashboard — the working surface. Handlers stay thin: all
DB access goes through ``expense_analyzer.queries``. Every route requires a
logged-in user, and the import list is scoped to the member who imported it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from expense_analyzer import iban
from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import AccountForm, CategoryEditForm, CategoryForm
from expense_analyzer.api.params import opt_int
from expense_analyzer.auth import require_user
from expense_analyzer.importers.pipeline import rollback_batch
from expense_analyzer.models import AccountType, CategoryKind, ImportBatch, Owner
from expense_analyzer.queries.categorize import categories
from expense_analyzer.queries.categorize.categories import HEX_COLOR_RE
from expense_analyzer.queries.core import accounts, users
from expense_analyzer.queries.money import batches
from expense_analyzer.queries.money.transactions import MANUAL_BATCH_SOURCE
from expense_analyzer.templating import templates

router = APIRouter(prefix="/dashboard", tags=["settings"], dependencies=[Depends(require_user)])


def _settings_context(session: Session, user: Owner, **extra) -> dict:
    members = users.list_users(session)

    return {
        "user": user,
        "accounts": accounts.list_accounts(session),
        "categories": categories.list_categories(session),
        "batches": batches.recent_batches(session, viewer_id=user.id),
        "account_types": [t.value for t in AccountType],
        "category_kinds": [k.value for k in CategoryKind],
        "owners": members,
        "owner_names": {m.id: m.name for m in members},
        # Re-render helpers: an error path passes the submitted AccountForm back so
        # the form keeps what the user typed; edit_id marks which row it belongs to.
        "account_form": None,
        "edit_id": None,
        **extra,
    }


def _parse_color(raw: str) -> tuple[str | None, str | None]:
    """Validate an "#rrggbb" string from the colour picker. Returns
    ``(colour, error)``: blank input is a valid "no colour" (``None``); anything
    that isn't a 6-digit hex colour is rejected so it never reaches the markup."""
    value = raw.strip().lower()

    if not value:
        return None, None

    if not HEX_COLOR_RE.match(value):
        return None, "Colour must be a hex value like #4f8cff."

    return value, None


def _parse_number(raw: str) -> tuple[str | None, str | None]:
    """Parse the optional account number / IBAN from the form. Returns
    ``(number, error)``: blank is a valid "no number" (``None``). A value shaped like
    an IBAN is stored canonically (upper, no spaces) and must pass the mod-97
    checksum, so a mistyped payment reference is caught. Anything else (cash box,
    brokerage id) is kept exactly as typed (only trimmed) — its case and separators
    can be meaningful, so we don't mangle it."""
    if not raw.strip():
        return None, None
    compact = iban.normalize(raw)

    if iban.looks_like_iban(compact):
        if not iban.is_valid(compact):
            return None, "That IBAN doesn't look valid — check the digits."

        return compact, None

    return raw.strip(), None


def _parse_owner(
    session: Session, raw: str, *, user: Owner, current_owner_id: int | None
) -> tuple[int | None, str | None]:
    """Parse and authorize the account's owner. Returns ``(owner_id, error)``.

    Only a genuinely blank value means "shared with the household". Text that is not
    a member id is rejected rather than coerced: this field moves a privacy boundary,
    and silently reading garbage as "shared" would turn a member's own account into
    one the whole house can see. The member must exist and be active — an account
    owned by a deactivated member would take imports nobody can reach, since they
    cannot log in and no one else may see their private rows.

    **Who may move it is the security question**, because the answer decides who owns
    everything imported into the account afterwards (see
    :func:`expense_analyzer.importers.pipeline.run_import`). Accounts are otherwise
    shared config any member may rename, so without this an ordinary member could
    point someone else's account at themselves, wait for them to upload their next
    statement, and take private ownership of every row in it. A member may therefore
    only claim an unowned account or release their own; moving anyone else's is the
    admin's, who needs it to reclaim an account after a member leaves.
    """
    owner_id: int | None = None

    if raw.strip():
        owner_id = opt_int(raw)

        # Unparseable is refused here rather than falling through as "unchanged" on a
        # shared account, where it would read as a deliberate "leave it shared".
        if owner_id is None:
            return None, "That member doesn't exist."

    # The form posts the current owner on every edit, so re-validating it here would
    # let a later deactivation freeze the whole record against renames.
    if owner_id == current_owner_id:
        return owner_id, None

    if owner_id is not None:
        owner = session.get(Owner, owner_id)

        if owner is None:
            return None, "That member doesn't exist."

        if not owner.is_active:
            return None, f"{owner.name} is deactivated — reactivate them first."

    if user.is_admin:
        return owner_id, None

    if current_owner_id not in (None, user.id):
        holder = session.get(Owner, current_owner_id)
        name = holder.name if holder else "another member"

        return None, f"That account is {name}'s — only they or an admin can hand it on."

    if owner_id not in (None, user.id):
        return None, "You can only take an account for yourself, or leave it shared."

    return owner_id, None


# Setup page (accounts, categories, recent imports). Lives at /dashboard/settings
# now that /dashboard itself is the overview — see overview.py.
@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "core/settings.html", _settings_context(session, user)
    )


@router.post("/accounts")
def create_account(
    request: Request, form: Annotated[AccountForm, Form()], user: CurrentUser, session: DbSession
) -> Response:
    # Name is the required field, so check it first — its error shouldn't be masked
    # by a number problem (mirrors create/edit category).
    number = None
    owner_id = None

    if not form.name.strip():
        error = "Account name can't be empty."
    else:
        number, error = _parse_number(form.number)

        if error is None:
            # A new account has no holder yet, so the only question is whether the
            # member is claiming it for themselves.
            owner_id, error = _parse_owner(session, form.owner_id, user=user, current_owner_id=None)

    if error is not None:
        return templates.TemplateResponse(
            request,
            "core/settings.html",
            _settings_context(session, user, error=error, account_form=form),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    accounts.create_account(
        session, name=form.name, type=form.type, number=number, owner_id=owner_id
    )

    return RedirectResponse("/dashboard/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/accounts/{account_id}/edit")
def edit_account(
    request: Request,
    account_id: int,
    form: Annotated[AccountForm, Form()],
    user: CurrentUser,
    session: DbSession,
) -> Response:
    number = None
    owner_id = None
    existing = accounts.get_account(session, account_id)

    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")

    if not form.name.strip():
        error = "Account name can't be empty."
    else:
        number, error = _parse_number(form.number)

        if error is None:
            owner_id, error = _parse_owner(
                session, form.owner_id, user=user, current_owner_id=existing.owner_id
            )

    if error is not None:
        return templates.TemplateResponse(
            request,
            "core/settings.html",
            _settings_context(session, user, error=error, account_form=form, edit_id=account_id),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Exists — the handler 404'd above, so update_account cannot miss.
    accounts.update_account(
        session,
        account_id,
        name=form.name,
        type=form.type,
        number=number,
        owner_id=owner_id,
    )

    return RedirectResponse("/dashboard/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/categories")
def create_category(
    request: Request, form: Annotated[CategoryForm, Form()], user: CurrentUser, session: DbSession
) -> Response:
    color, error = _parse_color(form.color)

    if error is not None:
        return templates.TemplateResponse(
            request,
            "core/settings.html",
            _settings_context(session, user, error=error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    categories.create_category(session, name=form.name, kind=form.kind, color=color)

    return RedirectResponse("/dashboard/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/categories/{category_id}/edit")
def edit_category(
    request: Request,
    category_id: int,
    form: Annotated[CategoryEditForm, Form()],
    user: CurrentUser,
    session: DbSession,
) -> Response:
    # Name is checked first so a colour problem can't mask a missing name.
    # "Clear" wins over whatever the picker holds.
    color, error = None, None

    if not form.name.strip():
        error = "Category name can't be empty."
    elif not form.clear:
        color, error = _parse_color(form.color)

    if error is not None:
        return templates.TemplateResponse(
            request,
            "core/settings.html",
            _settings_context(session, user, error=error),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if (
        categories.update_category(
            session, category_id, name=form.name, kind=form.kind, color=color
        )
        is None
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")

    return RedirectResponse("/dashboard/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/batches/{batch_id}/rollback")
def rollback(batch_id: int, user: CurrentUser, session: DbSession) -> RedirectResponse:
    # The Manual batch is a container for hand-entered rows, not a real import —
    # rolling it back would wipe every cash entry at once. Those are deleted one at a
    # time from the transactions list instead. Checked against the caller's own batch,
    # so a refusal never reveals whether someone else's batch exists.
    batch = session.get(ImportBatch, batch_id)

    if batch is None or (batch.owner_id is not None and batch.owner_id != user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such import batch")

    if batch.source == MANUAL_BATCH_SOURCE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="manual entries are deleted individually, not rolled back as a batch",
        )

    if rollback_batch(session, batch_id, viewer_id=user.id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such import batch")

    return RedirectResponse("/dashboard/settings", status_code=status.HTTP_303_SEE_OTHER)
