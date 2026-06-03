"""PKO BP CSV parser.

PKO BP exports ("Zestawienie operacji") are **windows-1250** encoded, comma
separated with quoted fields, and laid out as::

    Data operacji, Data waluty, Typ transakcji, Kwota, Waluta,
    Saldo po transakcji, Opis transakcji, <up to 6 more description fragments>

Amounts are signed with a dot decimal separator (``-321.51``, ``+20000.00``);
negative = expense, positive = inflow — exactly our convention.

Pending card authorizations ("Blokada") come through with an empty operation
date and a ``W rozliczeniu`` ("in settlement") balance. They are skipped: they
have no booking date and re-appear as real, dated rows once they settle.

Encoding is windows-1250 in practice, but we try UTF-8 first and fall back:
Polish cp1250 bytes are almost always invalid UTF-8, so a strict UTF-8 decode
raises and we retry as cp1250; a genuine UTF-8 export decodes cleanly instead of
being silently mojibaked.
"""

import csv
import io
from datetime import date

from expense_analyzer.importers.base import ImporterError, NormalizedTransaction, ParseResult
from expense_analyzer.money import MoneyParseError, parse_pln

_HEADER_FIRST_CELL = "Data operacji"

# Column indices in the PKO layout.
_OP_DATE = 0
_TYPE = 2
_AMOUNT = 3
_BALANCE = 5
_DESC_START = 6  # description fragments run from here to the end of the row


def _clean(text: str) -> str:
    """Strip and collapse internal whitespace (PKO pads fields generously)."""
    return " ".join(text.split())


def _decode(data: bytes) -> str:
    """Decode export bytes, preferring UTF-8 and falling back to cp1250."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1250")


class PKOCsvImporter:
    source = "PKO BP csv"

    def parse(self, data: bytes) -> ParseResult:
        reader = csv.reader(io.StringIO(_decode(data)), delimiter=",", quotechar='"')

        out: list[NormalizedTransaction] = []
        for row in reader:
            if len(row) <= _BALANCE or not any(cell.strip() for cell in row):
                continue  # blank or truncated line
            if row[_OP_DATE].strip() == _HEADER_FIRST_CELL:
                continue  # header

            op_date = row[_OP_DATE].strip()
            if not op_date:
                continue  # pending "Blokada" — no booking date yet

            # A malformed row fails the whole import (see ImporterError): silently
            # skipping it would drop a real transaction and under-count money.
            try:
                booked_date = date.fromisoformat(op_date)
                amount = parse_pln(row[_AMOUNT])
            except ValueError as exc:  # MoneyParseError is a ValueError too
                raise ImporterError(f"PKO CSV line {reader.line_num}: {exc}") from exc

            try:
                balance_after: int | None = parse_pln(row[_BALANCE])
            except MoneyParseError:
                balance_after = None  # e.g. "W rozliczeniu"

            tx_type = _clean(row[_TYPE])
            fragments = [_clean(cell) for cell in row[_DESC_START:] if cell.strip()]
            raw_description = " | ".join(part for part in [tx_type, *fragments] if part)

            out.append(
                NormalizedTransaction(
                    booked_date=booked_date,
                    amount=amount,
                    raw_description=raw_description,
                    balance_after=balance_after,
                )
            )

        # PKO reconciles via the per-row running balance, not declared totals,
        # so ParseResult carries no period totals — see reconciliation module.
        return ParseResult(transactions=out)
