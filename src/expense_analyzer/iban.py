"""IBAN validation (ISO 13616 mod-97 checksum).

Account numbers are stored as free-text reference data (see ``Account.number``), so
most values pass through untouched — a cash box or brokerage account has no IBAN.
But when a value *looks* like an IBAN (two country letters, two check digits, then
alphanumerics) we verify the checksum, so a mistyped payment reference is caught
before it's copied into a transfer. No dependency: mod-97 is a couple of lines.
"""

import re

# Two country letters, two check digits, then up to 30 alphanumerics (spaces stripped).
_IBAN_SHAPE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{1,30}$")


def normalize(raw: str) -> str:
    """Collapse all whitespace and uppercase — the canonical stored form."""
    return "".join(raw.split()).upper()


def looks_like_iban(value: str) -> bool:
    """True if ``value`` (already normalized) has the IBAN shape — worth checksumming."""
    return bool(_IBAN_SHAPE.match(value))


def is_valid(value: str) -> bool:
    """True if a normalized, IBAN-shaped ``value`` passes the ISO 13616 mod-97 check.

    Move the four leading chars to the end, map each char to a number (0-9 stay,
    A-Z -> 10-35 via base-36), read the result as one big integer: a valid IBAN
    leaves remainder 1 mod 97."""
    rearranged = value[4:] + value[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)

    return int(digits) % 97 == 1
