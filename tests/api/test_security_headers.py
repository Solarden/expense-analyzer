"""Guard: every response carries the security headers.

The app sets them itself, so a deployment behind a proxy that sets none is still covered.
"""

import pytest
from fastapi.testclient import TestClient

from expense_analyzer.main import SECURITY_HEADERS


@pytest.mark.parametrize("path", ["/login", "/dashboard"])
def test_every_response_carries_the_security_headers(client: TestClient, path: str) -> None:
    response = client.get(path, follow_redirects=False)

    assert {name: response.headers.get(name) for name in SECURITY_HEADERS} == SECURITY_HEADERS
