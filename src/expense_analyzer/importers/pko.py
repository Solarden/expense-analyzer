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
"""

import csv
import io
from datetime import date

from expense_analyzer.importers.base import NormalizedTransaction
from expense_analyzer.money import MoneyParseError, parse_pln

_ENCODING = "cp1250"
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


class PKOCsvImporter:
    source = "PKO BP csv"

    def parse(self, data: bytes) -> list[NormalizedTransaction]:
        text = data.decode(_ENCODING)
        reader = csv.reader(io.StringIO(text), delimiter=",", quotechar='"')

        out: list[NormalizedTransaction] = []
        for row in reader:
            if len(row) <= _BALANCE or not any(cell.strip() for cell in row):
                continue  # blank or truncated line
            if row[_OP_DATE].strip() == _HEADER_FIRST_CELL:
                continue  # header

            op_date = row[_OP_DATE].strip()
            if not op_date:
                continue  # pending "Blokada" — no booking date yet

            amount = parse_pln(row[_AMOUNT])

            try:
                balance_after: int | None = parse_pln(row[_BALANCE])
            except MoneyParseError:
                balance_after = None  # e.g. "W rozliczeniu"

            tx_type = _clean(row[_TYPE])
            fragments = [_clean(cell) for cell in row[_DESC_START:] if cell.strip()]
            raw_description = " | ".join(part for part in [tx_type, *fragments] if part)

            out.append(
                NormalizedTransaction(
                    booked_date=date.fromisoformat(op_date),
                    amount=amount,
                    raw_description=raw_description,
                    balance_after=balance_after,
                )
            )
        return out
