"""Merchant normalization (design §5).

A shared, bank-agnostic heuristic that turns a raw bank description into a short,
stable merchant label. It is deliberately **best-effort**: the categorization
rules layer (roadmap §10) will lean on ``merchant_normalized``, but nothing today
depends on it being perfect, and it never affects the import fingerprint (which
hashes ``raw_description``). Returns ``None`` when no meaningful merchant survives.

The heuristics key off textual markers that appear in the normalized descriptions
of both supported banks, so the logic stays in one place rather than per parser:

- PKO card payments embed the merchant in a ``Lokalizacja: Adres: <name> Miasto:``
  fragment; transfers name the counterparty in ``Nazwa nadawcy/odbiorcy: <name>``.
- mBank prints the counterparty (and its address) at the very start of the
  description, before the first comma.
"""

import re

# PKO card payments: the merchant name sits between "Adres:" and "Miasto:".
_PKO_ADDRESS = re.compile(r"Adres:\s*(.+?)\s+Miasto:", re.IGNORECASE)

# PKO transfers: the counterparty is the "Nazwa nadawcy/odbiorcy" field value.
_PKO_PARTY = re.compile(r"Nazwa (?:nadawcy|odbiorcy):\s*(.+?)(?:\s*\||$)", re.IGNORECASE)

# Noise tokens that are never part of a merchant name.
_NOISE = [
    re.compile(r"\d{6}\*{2,}\d{4}"),  # masked card number, 400000******0000
    re.compile(r"\b\d{2}(?:\s?\d{4}){6}\b"),  # spaced 26-digit IBAN/account
    re.compile(r"\b\d{16,}\b"),  # long account/reference digit runs
    re.compile(r"\b\d{2}-\d{3}\b"),  # PL postal code, 90-451
]


def normalize_merchant(raw_description: str) -> str | None:
    text = raw_description.strip()
    if not text:
        return None

    address = _PKO_ADDRESS.search(text)
    party = _PKO_PARTY.search(text)
    if address:
        candidate = address.group(1)
    elif party:
        candidate = party.group(1)
    else:
        # mBank (and PKO fallback): take the head, before the first comma or the
        # pipe-joined label fragments PKO appends.
        candidate = text.split(" | ", 1)[0].split(",", 1)[0]

    for pattern in _NOISE:
        candidate = pattern.sub(" ", candidate)

    candidate = " ".join(candidate.split()).strip(" -,/.").upper()
    if len(candidate) < 2:
        return None

    # Merchant names are short; the tail is usually address noise we kept.
    return candidate[:80]
