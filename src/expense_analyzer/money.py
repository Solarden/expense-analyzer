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


def _clean_numeric(text: str) -> str | None:
    """Normalize an external numeric string to a bare ``Decimal``-parseable form.

    Strips the quirks investment sources throw at us (myFund returns numbers as
    strings, sometimes with a leading ``+``; XTB .xlsx stores them with a ``.``
    decimal): thousands spaces / non-breaking spaces, ``zł``/``PLN`` suffixes, a
    leading ``+``, and ``,`` used as a decimal separator. Returns ``None`` for
    missing/blank markers (``""``, ``&nbsp;``, ``---``, a lone sign/dash).
    """
    cleaned = (
        text.replace("&nbsp;", " ")
        .replace("\xa0", " ")
        .replace(" ", "")  # narrow no-break space
        .strip()
        .replace(" ", "")  # thousands separators
        .replace("zł", "")
        .replace("PLN", "")
        .lstrip("+")
    )
    if cleaned in ("", "-", "--", "---", "—"):
        return None

    # Resolve the decimal separator. When BOTH ``.`` and ``,`` appear (e.g.
    # ``"1,234.56"`` or ``"1.234,56"``), the **rightmost** one is the decimal
    # point and the other groups thousands — strip the thousands char. When only
    # one appears, treat ``,`` as the decimal separator (Polish convention).
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(",", ".")

    return cleaned


def parse_loose_amount(value: str | int | float | None) -> int | None:
    """Best-effort parse of an external numeric value into signed minor units.

    For investment sources (myFund API, XTB export) whose numbers arrive as
    strings. Returns ``None`` for missing/blank values. Never routes through a
    binary ``float``: a ``float`` input is stringified before going to
    :class:`~decimal.Decimal`, so JSON's ``632.56`` stays exact. Raises
    :class:`MoneyParseError` on genuinely unparseable text.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass — reject it explicitly
        raise MoneyParseError(f"not a numeric amount: {value!r}")
    if isinstance(value, int):
        return to_minor_units(value)
    if isinstance(value, float):
        return to_minor_units(Decimal(str(value)))

    cleaned = _clean_numeric(value)
    if cleaned is None:
        return None
    try:
        return to_minor_units(Decimal(cleaned))
    except (InvalidOperation, ArithmeticError) as exc:
        raise MoneyParseError(f"cannot parse amount: {value!r}") from exc


def parse_loose_decimal(value: str | int | float | None) -> Decimal | None:
    """Like :func:`parse_loose_amount` but for **counts**, not money.

    Share/unit quantities are genuinely fractional and are *not* minor units, so
    they stay a :class:`~decimal.Decimal` (e.g. ``"0.1980"`` -> ``Decimal('0.1980')``).
    Returns ``None`` for missing/blank values.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise MoneyParseError(f"not a numeric quantity: {value!r}")
    if isinstance(value, int | float):
        return Decimal(str(value))

    cleaned = _clean_numeric(value)
    if cleaned is None:
        return None
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ArithmeticError) as exc:
        raise MoneyParseError(f"cannot parse quantity: {value!r}") from exc


def from_minor_units(minor: int) -> Decimal:
    """Convert integer minor units back to a major-unit (PLN) :class:`~decimal.Decimal`."""
    return (Decimal(minor) / _MINOR_UNITS_PER_PLN).quantize(Decimal("0.01"))


def format_pln(minor: int) -> str:
    """Format integer minor units for display, e.g. ``-123456`` -> ``"-1 234,56 zł"``.

    Polish convention: thousands grouped with a non-breaking space (so the number
    never wraps mid-amount), comma as the decimal separator. Computed straight from
    the integer minor units — never via a binary float.
    """
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(minor), 100)
    grouped = f"{whole:,}".replace(",", " ")

    return f"{sign}{grouped},{frac:02d} zł"


def format_quantity(quantity: Decimal) -> str:
    """Display a unit count without trailing zeros (``2.0000`` -> ``"2"``,
    ``0.1980`` -> ``"0.198"``). ``:f`` avoids ``normalize()``'s exponent form."""
    return f"{quantity.normalize():f}"
