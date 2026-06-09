"""Updates page (/dashboard/updates): show whether a newer release is waiting.

Read-only and offline by design. The network egress lives entirely in the cron
check on the Pi host (``scripts/check_update.sh`` → ``ha.update_notify``), which
writes its verdict to ``settings.update_status_path``. This page only *reads* that
file — it never fetches anything and never deploys (notify-only; the owner runs
``make deploy`` by hand). See keep-pi-fully-local + updater-notify-only.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from expense_analyzer.api.deps import CurrentUser
from expense_analyzer.auth import require_user
from expense_analyzer.config import get_settings
from expense_analyzer.ha.update_notify import load_status
from expense_analyzer.templating import templates

router = APIRouter(prefix="/dashboard", tags=["updates"], dependencies=[Depends(require_user)])


@router.get("/updates", response_class=HTMLResponse)
def updates_page(request: Request, user: CurrentUser) -> HTMLResponse:
    settings = get_settings()
    status = load_status(settings.update_status_path)

    # A plain link to the release on the source host — rendered as an <a href>, so
    # the app itself makes no request; the user's browser opens it if they click.
    # The /releases/tag/<v> path is GitHub-shaped, so only build it when source_url
    # actually points at GitHub; a fork on another host just gets no release link
    # (the page handles release_url being None) rather than a wrong one.
    release_url = None
    if status is not None and status.latest and "github.com" in settings.source_url:
        release_url = f"{settings.source_url.rstrip('/')}/releases/tag/{status.latest}"

    return templates.TemplateResponse(
        request,
        "core/updates.html",
        {"user": user, "status": status, "release_url": release_url},
    )
