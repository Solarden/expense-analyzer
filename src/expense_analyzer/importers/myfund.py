"""myFund.pl API client (design §7.3) — the opt-in network positions source.

This is the **only** outbound network call in the app. It is gated behind
configuration (``EA_MYFUND_API_KEY`` + ``EA_MYFUND_PORTFOLIO``): with no key the
client is never constructed and the app stays fully offline. The pull is
read-only — it fetches the user's own portfolio composition.

The API (see internal_docs/APIdoc.yaml) returns many numbers as strings, some
with a leading ``+``, and is multi-currency. We parse everything defensively via
:func:`~expense_analyzer.money.parse_loose_amount` and keep money in minor units.
``status.code`` is a string: ``"0"`` ok, ``"1"`` error, ``"7"`` portfolio not
found — anything but ``"0"`` raises :class:`MyFundError`.
"""

from datetime import date
from decimal import Decimal

import httpx

from expense_analyzer.clock import local_today
from expense_analyzer.config import Settings
from expense_analyzer.importers.positions import NormalizedPosition, PositionsResult
from expense_analyzer.money import parse_loose_amount, parse_loose_decimal

_ZERO = Decimal(0)

_ENDPOINT = "/getPortfel.php"
_TIMEOUT_SECONDS = 20.0


class MyFundError(Exception):
    """A myFund API call failed (network, HTTP, or a non-zero ``status.code``)."""


class MyFundClient:
    """Fetches a portfolio snapshot from the myFund.pl API.

    ``transport`` is injectable so tests can drive it with an
    :class:`httpx.MockTransport` instead of hitting the network.
    """

    source = "myFund.pl API"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        portfolio: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._portfolio = portfolio
        self._transport = transport

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx.BaseTransport | None = None
    ) -> "MyFundClient":
        if not settings.myfund_configured:
            raise MyFundError(
                "myFund is not configured — set EA_MYFUND_API_KEY and EA_MYFUND_PORTFOLIO."
            )
        return cls(
            base_url=settings.myfund_api_base_url,
            api_key=settings.myfund_api_key.get_secret_value(),
            portfolio=settings.myfund_portfolio,
            transport=transport,
        )

    def fetch(self) -> PositionsResult:
        """Pull the portfolio and map it to a :class:`PositionsResult`."""
        params = {"portfel": self._portfolio, "apiKey": self._api_key, "format": "json"}
        try:
            # ┌─ SECURITY: do NOT enable follow_redirects here. ─────────────────┐
            # │ The API key travels as a query parameter (myFund's design), so   │
            # │ it is embedded in the request URL. If redirects were followed, a │
            # │ malicious or compromised myFund host could 30x-redirect us to an │
            # │ attacker-controlled URL and httpx would replay the apiKey-bearing│
            # │ request there — leaking the key and turning this into an SSRF /  │
            # │ secret-exfiltration primitive. httpx defaults to                 │
            # │ follow_redirects=False; we set it explicitly so the intent is    │
            # │ unmistakable and survives refactors. Keep it False.              │
            # └──────────────────────────────────────────────────────────────────┘
            with httpx.Client(
                base_url=self._base_url,
                timeout=_TIMEOUT_SECONDS,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.get(_ENDPOINT, params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            # Don't leak the URL (it carries the apiKey as a query param) into the message.
            raise MyFundError(f"myFund request failed: {type(exc).__name__}") from exc
        except ValueError as exc:  # JSON decode
            raise MyFundError("myFund returned a non-JSON response") from exc

        return _parse_portfolio(payload)


def _parse_portfolio(payload: dict) -> PositionsResult:
    status = payload.get("status") or {}
    code = str(status.get("code", ""))
    if code != "0":
        text = status.get("text") or "unknown error"
        if code == "7":
            raise MyFundError(f"portfolio not found: {text}")
        raise MyFundError(f"myFund error (code {code}): {text}")

    summary = payload.get("portfel") or {}
    currency = (summary.get("waluta") or "PLN").strip() or "PLN"
    snapshot = _parse_api_date(summary.get("data"))

    positions: list[NormalizedPosition] = []
    for ticker in (payload.get("tickers") or {}).values():
        symbol = (ticker.get("tickerClear") or "").strip()
        if not symbol:
            continue
        value = parse_loose_amount(ticker.get("wartosc"))
        if value is None:
            continue
        positions.append(
            NormalizedPosition(
                ticker=symbol,
                quantity=parse_loose_decimal(ticker.get("liczbaJednostek")) or _ZERO,
                value=value,
                snapshot_date=snapshot,
                avg_price=parse_loose_amount(ticker.get("cenaZakupu")),
                current_price=parse_loose_amount(ticker.get("close")),
                currency=currency,
            )
        )

    return PositionsResult(
        positions=positions,
        declared_total=parse_loose_amount(summary.get("wartosc")),
        cash_balance=None,
    )


def _parse_api_date(value: object) -> date:
    """Parse the API's ``YYYY-MM-DD`` date; fall back to today on ``&nbsp;``/blank."""
    if isinstance(value, str):
        text = value.strip()
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass

    return local_today()
