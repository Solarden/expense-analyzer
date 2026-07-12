"""Shared FastAPI dependencies as typed aliases, to keep handler signatures lean.

``CurrentUser`` and ``DbSession`` replace the repeated
``user: Owner = Depends(require_user)`` / ``session: Session = Depends(get_session)``
boilerplate that every handler would otherwise carry.

Note: an ``Annotated[...]`` dependency has no default value, so a parameter typed
with one of these must come **before** any parameter that does have a default
(query/Form params) in a handler signature.
"""

from typing import Annotated

from fastapi import Depends, Request
from sqlmodel import Session

from expense_analyzer.auth import require_admin, require_user
from expense_analyzer.db import get_session
from expense_analyzer.models import Lens, Owner
from expense_analyzer.queries.visibility import resolve_lens

DbSession = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[Owner, Depends(require_user)]
AdminUser = Annotated[Owner, Depends(require_admin)]


def current_lens(request: Request) -> Lens:
    """The viewer's active analytical lens (All / Private / Home budget).

    A ``?lens=`` param (from the global switcher) is persisted to the session so it
    sticks across pages; otherwise the stored value is used, else the safe default.
    """
    raw = request.query_params.get("lens")
    if raw is not None and "session" in request.scope:
        request.session["lens"] = resolve_lens(raw).value
    stored = request.session.get("lens") if "session" in request.scope else None

    return resolve_lens(stored)


CurrentLens = Annotated[Lens, Depends(current_lens)]
