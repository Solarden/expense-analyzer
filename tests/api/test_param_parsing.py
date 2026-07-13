"""Regression: leniently-parsed query params must never 500 on crafted input.

A "²" (Unicode superscript) passes ``str.isdigit()`` but ``int()`` rejects it;
before the ``opt_int()`` helper this raised an unhandled ``ValueError`` -> HTTP
500. Every endpoint that leniently parses an optional id/page param should fall
back to its default and render 200. Guards all call sites at once (they route
through the one helper), so a future re-introduction of the raw idiom is caught.
"""

import pytest
from fastapi import status
from fastapi.testclient import TestClient

# Each URL carries a "²" in a leniently-parsed param — the value str.isdigit()
# accepts but int() cannot parse.
_CRAFTED_URLS = [
    "/dashboard/transactions?category=²",
    "/dashboard/transactions?account_id=²",
    "/dashboard/transactions?page=²",
    "/dashboard/transactions?size=²",
    "/dashboard/transactions?lens=home&added_by=²",
    "/dashboard/budgets?edit=²",
    "/dashboard/plan?edit=²",
    "/dashboard/queue?page=²",
]


@pytest.mark.parametrize("url", _CRAFTED_URLS)
def test_exotic_digit_param_falls_back_not_500(auth_client: TestClient, url: str):
    resp = auth_client.get(url)

    assert resp.status_code == status.HTTP_200_OK
