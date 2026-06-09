"""Import CSV page: upload a bank export and run the import (design §6).

A bad file is a normal user mistake (wrong bank/format, a malformed row), so the
parser's :class:`ImporterError` becomes a red flash, not a 500.
"""

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.auth import require_user
from expense_analyzer.importers import ImporterError, run_import
from expense_analyzer.importers.registry import available, get_importer
from expense_analyzer.queries.core import accounts
from expense_analyzer.templating import templates

router = APIRouter(
    prefix="/dashboard/upload", tags=["import"], dependencies=[Depends(require_user)]
)


@router.get("", response_class=HTMLResponse)
def upload_form(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "core/upload.html",
        {"user": user, "accounts": accounts.list_accounts(session), "importers": available()},
    )


@router.post("", response_class=HTMLResponse)
async def upload(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    account_id: int = Form(...),
    importer: str = Form(...),
    file: UploadFile = File(...),
) -> HTMLResponse:
    context: dict = {
        "user": user,
        "accounts": accounts.list_accounts(session),
        "importers": available(),
    }

    if importer not in available():
        context["error"] = f"Unknown importer: {importer!r}."
    elif accounts.get_account(session, account_id) is None:
        context["error"] = f"Unknown account #{account_id}."
    else:
        data = await file.read()
        try:
            summary = run_import(
                session,
                account_id=account_id,
                importer=get_importer(importer),
                filename=file.filename or "upload.csv",
                data=data,
            )
        except ImporterError as exc:
            # Wrong bank/format or a malformed row — a normal user mistake, not a crash.
            context["error"] = f"Could not parse the file: {exc}"
        else:
            context["summary"] = summary
            context["account_id"] = account_id
            context["flash"] = f"Imported: {summary.new} new, {summary.skipped} skipped."

    return templates.TemplateResponse(request, "core/upload.html", context)
