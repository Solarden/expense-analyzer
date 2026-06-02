"""Money handling.

Policy (design §5): money is stored as **integer minor units (grosze)**, never
float. 100 zł == ``10000``. Summing hundreds of floats drifts by a grosz and
balances stop reconciling — so we parse to :class:`~decimal.Decimal` only at the
edges (CSV in, display out) and keep ``int`` grosze everywhere in between.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_GROSZE_PER_ZLOTY = Decimal(100)


class MoneyParseError(ValueError):
    """Raised when a text amount cannot be parsed into grosze."""


def to_grosze(amount: Decimal | int | str) -> int:
    """Convert a złoty amount to integer grosze, rounding half-up.

    Accepts a :class:`~decimal.Decimal`, an ``int`` (whole złoty) or a plain
    numeric string like ``"123.45"``. For locale-formatted bank strings
    (``"1 234,56 zł"``) use :func:`parse_pln` instead.
    """
    if isinstance(amount, str):
        amount = Decimal(amount)
    grosze = (Decimal(amount) * _GROSZE_PER_ZLOTY).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(grosze)


def parse_pln(text: str) -> int:
    """Parse a Polish-formatted money string into signed integer grosze.

    Handles what Polish bank CSVs throw at us: comma decimal separator, space /
    non-breaking-space thousands separators, a trailing ``zł``/``PLN`` and a
    leading/trailing sign. ``"-1 234,56 zł"`` -> ``-123456``.
    """
    cleaned = (
        text.strip()
        .replace("\xa0", "")  # non-breaking space (common in PL exports)
        .replace(" ", "")  # narrow no-break space
        .replace(" ", "")
        .replace("zł", "")
        .replace("PLN", "")
        .replace(",", ".")
        .strip()
    )
    if not cleaned:
        raise MoneyParseError(f"empty money value: {text!r}")
    try:
        return to_grosze(Decimal(cleaned))
    except (InvalidOperation, ArithmeticError) as exc:
        raise MoneyParseError(f"cannot parse money value: {text!r}") from exc


def from_grosze(grosze: int) -> Decimal:
    """Convert integer grosze back to a złoty :class:`~decimal.Decimal`."""
    return (Decimal(grosze) / _GROSZE_PER_ZLOTY).quantize(Decimal("0.01"))


def format_pln(grosze: int) -> str:
    """Format integer grosze for display, e.g. ``-123456`` -> ``"-1234,56 zł"``."""
    return f"{from_grosze(grosze)} zł".replace(".", ",")
