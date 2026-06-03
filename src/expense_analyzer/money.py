"""Money handling.

Policy (design §5): money is stored as **integer minor units**, never float.
100 PLN == ``10000``. Summing hundreds of floats drifts by a minor unit and
balances stop reconciling — so we parse to :class:`~decimal.Decimal` only at the
edges (CSV in, display out) and keep ``int`` minor units everywhere in between.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_MINOR_UNITS_PER_PLN = Decimal(100)


class MoneyParseError(ValueError):
    """Raised when a text amount cannot be parsed into minor units."""


def to_minor_units(amount: Decimal | int | str) -> int:
    """Convert a major-unit (PLN) amount to integer minor units, rounding half-up.

    Accepts a :class:`~decimal.Decimal`, an ``int`` (whole PLN) or a plain
    numeric string like ``"123.45"``. For locale-formatted bank strings
    (``"1 234,56 zł"``) use :func:`parse_pln` instead.
    """
    if isinstance(amount, str):
        amount = Decimal(amount)
    minor = (Decimal(amount) * _MINOR_UNITS_PER_PLN).quantize(Decimal(1), rounding=ROUND_HALF_UP)

    return int(minor)


def parse_pln(text: str) -> int:
    """Parse a Polish-formatted money string into signed integer minor units.

    Handles what Polish bank CSVs throw at us: comma decimal separator, space /
    non-breaking-space thousands separators, a trailing ``zł``/``PLN`` and a
    leading/trailing sign. ``"-1 234,56 zł"`` -> ``-123456``.
    """
    cleaned = (
        text.strip()
        .replace("\xa0", "")  # non-breaking space (common in PL exports)
        .replace(" ", "")  # narrow no-break space
        .replace(" ", "")
        .replace("zł", "")
        .replace("PLN", "")
        .replace(",", ".")
        .strip()
    )
    if not cleaned:
        raise MoneyParseError(f"empty money value: {text!r}")
    try:
        return to_minor_units(Decimal(cleaned))
    except (InvalidOperation, ArithmeticError) as exc:
        raise MoneyParseError(f"cannot parse money value: {text!r}") from exc


def from_minor_units(minor: int) -> Decimal:
    """Convert integer minor units back to a major-unit (PLN) :class:`~decimal.Decimal`."""
    return (Decimal(minor) / _MINOR_UNITS_PER_PLN).quantize(Decimal("0.01"))


def format_pln(minor: int) -> str:
    """Format integer minor units for display, e.g. ``-123456`` -> ``"-1234,56 zł"``."""
    return f"{from_minor_units(minor)} zł".replace(".", ",")
