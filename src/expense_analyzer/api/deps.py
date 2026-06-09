"""Shared FastAPI dependencies as typed aliases, to keep handler signatures lean.

``CurrentUser`` and ``DbSession`` replace the repeated
``user: Owner = Depends(require_user)`` / ``session: Session = Depends(get_session)``
boilerplate that every handler would otherwise carry.

Note: an ``Annotated[...]`` dependency has no default value, so a parameter typed
with one of these must come **before** any parameter that does have a default
(query/Form params) in a handler signature.
"""

from typing import Annotated

from fastapi import Depends
from sqlmodel import Session

from expense_analyzer.auth import require_admin, require_user
from expense_analyzer.db import get_session
from expense_analyzer.models import Owner

DbSession = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[Owner, Depends(require_user)]
AdminUser = Annotated[Owner, Depends(require_admin)]
