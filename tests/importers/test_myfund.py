"""myFund.pl API client tests — driven by an httpx MockTransport (no network)."""

from datetime import date
from decimal import Decimal

import httpx
import pytest

from expense_analyzer.importers.myfund import MyFundClient, MyFundError

_OK_PAYLOAD = {
    "status": {"code": "0", "text": "OK"},
    "portfel": {"waluta": "PLN", "data": "2026-04-15", "wartosc": "1265.12"},
    "tickers": {
        "7": {
            "tickerClear": "SXR8.DE",
            "liczbaJednostek": "2",
            "wartosc": "632.56",
            "cenaZakupu": "580.75",
            "close": "316.28",
        },
        "1": {
            "tickerClear": "SNT.PL",
            "liczbaJednostek": "3",
            "wartosc": "+632.56",  # API may prefix gains with '+'
            "cenaZakupu": "200.00",
            "close": "210.85",
        },
    },
}


def _client(payload: dict, *, status_code: int = 200) -> MyFundClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    return MyFundClient(
        base_url="https://myfund.pl/API/v1",
        api_key="secret",
        portfolio="Mój Portfel",
        transport=httpx.MockTransport(handler),
    )


def test_maps_tickers_to_positions() -> None:
    result = _client(_OK_PAYLOAD).fetch()

    by_ticker = {p.ticker: p for p in result.positions}
    assert set(by_ticker) == {"SXR8.DE", "SNT.PL"}

    sxr8 = by_ticker["SXR8.DE"]
    assert sxr8.quantity == Decimal("2")
    assert sxr8.value == 63256
    assert sxr8.avg_price == 58075
    assert sxr8.current_price == 31628
    assert sxr8.currency == "PLN"
    assert sxr8.snapshot_date == date(2026, 4, 15)

    # The '+' prefix on a gain parses cleanly.
    assert by_ticker["SNT.PL"].value == 63256


def test_declared_total_is_portfolio_value() -> None:
    result = _client(_OK_PAYLOAD).fetch()

    assert result.declared_total == 126512


def test_status_code_7_raises_not_found() -> None:
    payload = {"status": {"code": "7", "text": "Portfel nie znaleziony"}}
    with pytest.raises(MyFundError, match="not found"):
        _client(payload).fetch()


def test_status_code_1_raises_error() -> None:
    payload = {"status": {"code": "1", "text": "Zły klucz"}}
    with pytest.raises(MyFundError):
        _client(payload).fetch()


def test_http_error_is_wrapped() -> None:
    with pytest.raises(MyFundError):
        _client({}, status_code=500).fetch()


def test_does_not_follow_redirects() -> None:
    """SECURITY REGRESSION GUARD — do not weaken this test.

    The myFund API key rides in the request URL (a query parameter). If the client
    followed HTTP redirects, a malicious/compromised host could 30x-redirect us to
    an attacker-controlled URL and httpx would replay the apiKey-bearing request
    there — leaking the key (SSRF / secret exfiltration). ``MyFundClient`` therefore
    sets ``follow_redirects=False`` explicitly. This test fails the moment someone
    flips that: it asserts a 302 is NOT chased to its ``Location``.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("getPortfel.php"):
            # If redirects were followed, httpx would issue a *second* request to
            # this Location through the same transport — which the asserts below
            # would catch.
            return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
        return httpx.Response(200, json=_OK_PAYLOAD)  # the "evil" target — must never be hit

    client = MyFundClient(
        base_url="https://myfund.pl/API/v1",
        api_key="secret",
        portfolio="P",
        transport=httpx.MockTransport(handler),
    )
    # The unfollowed 302 has no JSON body, so fetch() surfaces a MyFundError.
    with pytest.raises(MyFundError):
        client.fetch()

    assert len(seen) == 1, "redirect was followed — follow_redirects must stay False"
    assert all("evil.example" not in url for url in seen), (
        "apiKey-bearing request hit redirect target"
    )


def test_blank_api_date_falls_back_to_today() -> None:
    payload = {
        "status": {"code": "0"},
        "portfel": {"waluta": "PLN", "data": "&nbsp;", "wartosc": "10.00"},
        "tickers": {"1": {"tickerClear": "X.PL", "liczbaJednostek": "1", "wartosc": "10.00"}},
    }
    result = _client(payload).fetch()

    assert len(result.positions) == 1  # parsed despite the missing date
