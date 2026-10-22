"""Guard: every response carries the security headers.

The app sets them itself, so a deployment behind a proxy that sets none is still covered.
"""

import re
from collections.abc import Callable
from datetime import date

import pytest
from fastapi.testclient import TestClient

from expense_analyzer.main import SECURITY_HEADERS
from expense_analyzer.models import Account, Transaction


@pytest.mark.parametrize("path", ["/login", "/dashboard"])
def test_every_response_carries_the_security_headers(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)

    assert {name: response.headers.get(name) for name in SECURITY_HEADERS} == SECURITY_HEADERS


# The CSP allows scripts from 'self' only, so an inline handler or <script> block would be dead
# code in the browser while every server-side test still passes.
INLINE_SCRIPT = re.compile(r'<script(?![^>]*\b(?:src=|type="application/json"))|\son[a-z]+="')


@pytest.mark.parametrize(
    "path",
    [
        "/dashboard/stats?month=2026-05",
        "/dashboard/transactions",
        "/dashboard/plan",
        "/dashboard/settings",
        "/dashboard/net-worth",
        "/dashboard/users",
    ],
)
def test_pages_carry_no_inline_script(
    auth_client: TestClient,
    account: Account,
    make_transaction: Callable[..., Transaction],
    path: str,
) -> None:
    make_transaction(account_id=account.id, amount=-7500, booked_date=date(2026, 5, 3))

    body = auth_client.get(path).text

    assert INLINE_SCRIPT.findall(body) == []
