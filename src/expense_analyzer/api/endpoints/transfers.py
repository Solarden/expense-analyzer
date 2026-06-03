"""Transfers page: review, confirm and unlink internal transfers (design §7.2).

GET is read-only — suggestions are recomputed live, so it never mutates state;
auto-linking happens only on import or an explicit rescan.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.api.forms import TransferConfirmForm
from expense_analyzer.auth import require_user
from expense_analyzer.config import get_settings
from expense_analyzer.queries import accounts
from expense_analyzer.queries import transfers as transfer_queries
from expense_analyzer.templating import templates
from expense_analyzer.transfers import find_transfer_pairs

router = APIRouter(
    prefix="/dashboard/transfers", tags=["transfers"], dependencies=[Depends(require_user)]
)


@router.get("", response_class=HTMLResponse)
def transfers_page(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    flash: str | None = None,
) -> HTMLResponse:
    # Suggestions are recomputed live (read-only) so a GET never mutates state;
    # auto-linking happens only on import or an explicit rescan.
    result = find_transfer_pairs(
        transfer_queries.unmatched_candidates(session),
        window_days=get_settings().transfer_window_days,
    )

    return templates.TemplateResponse(
        request,
        "transfers.html",
        {
            "user": user,
            "suggestions": result.ambiguous,
            "groups": transfer_queries.list_transfer_groups(session),
            "accounts": {a.id: a.name for a in accounts.list_accounts(session)},
            "flash": flash,
        },
    )


@router.post("/confirm")
def confirm_transfer(
    form: Annotated[TransferConfirmForm, Form()],
    session: DbSession,
) -> RedirectResponse:
    if transfer_queries.link_transfer(session, tx_a_id=form.tx_a_id, tx_b_id=form.tx_b_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="not a valid transfer pair"
        )

    return RedirectResponse("/dashboard/transfers", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/rescan")
def rescan_transfers(session: DbSession) -> RedirectResponse:
    linked, _ = transfer_queries.detect_and_autolink(
        session, window_days=get_settings().transfer_window_days
    )

    return RedirectResponse(
        f"/dashboard/transfers?flash=Auto-linked+{linked}+transfer(s).",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{group_id}/unlink")
def unlink_transfer(group_id: str, session: DbSession) -> RedirectResponse:
    transfer_queries.unlink_transfer(session, group_id)

    return RedirectResponse("/dashboard/transfers", status_code=status.HTTP_303_SEE_OTHER)
