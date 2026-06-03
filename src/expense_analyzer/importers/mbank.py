"""mBank CSV parser.

mBank "Lista operacji" exports differ from PKO in every way that matters, which
is exactly why each bank gets its own parser (design §6):

- **Encoding** is UTF-8 with a BOM in current exports; older ones were
  ``windows-1250``. We decode UTF-8 first (stripping the BOM) and fall back to
  cp1250, mirroring PKO's strategy.
- **Separator** is a semicolon, fields are quoted, lines end with CRLF.
- **Layout** has a long human-readable preamble (bank address, account holder,
  period, per-currency totals) before the transaction table. The table starts
  at the ``#Data operacji`` header row and its columns are::

      Data operacji ; Opis operacji ; Rachunek ; Kategoria ; Kwota

  Amounts are Polish-formatted with a ``PLN`` suffix (``-88 130,08 PLN``);
  :func:`~expense_analyzer.money.parse_pln` handles the sign, spaces and comma.

mBank does **not** print a running balance per row, so reconciliation cannot use
balance continuity here. Instead the preamble declares the period totals::

    #Waluta ; #Wpływy ; #Wydatki
    PLN     ; 88 053,00 ; -88 130,08

We surface those as ``ParseResult.declared_inflow`` / ``declared_outflow`` so the
reconciler can check the parsed sums against what the bank reported.
"""

import csv
import io
import re
from datetime import date

from expense_analyzer.importers.base import ImporterError, NormalizedTransaction, ParseResult
from expense_analyzer.money import MoneyParseError, parse_pln

# Column indices in the mBank transaction table.
_OP_DATE = 0
_DESCRIPTION = 1
_AMOUNT = 4

_COLUMN_HEADER_FIRST_CELL = "#Data operacji"
_TOTALS_HEADER_FIRST_CELL = "#Waluta"

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean(text: str) -> str:
    """Strip and collapse internal whitespace (mBank pads descriptions heavily)."""
    return " ".join(text.split())


def _decode(data: bytes) -> str:
    """Decode export bytes, preferring UTF-8 (BOM-aware) and falling back to cp1250."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1250")


def _parse_total(cell: str) -> int | None:
    """Parse a declared-total cell, tolerating a missing/blank value."""
    try:
        return parse_pln(cell)
    except MoneyParseError:
        return None


class MBankCsvImporter:
    source = "mBank csv"

    def parse(self, data: bytes) -> ParseResult:
        reader = csv.reader(io.StringIO(_decode(data)), delimiter=";", quotechar='"')

        out: list[NormalizedTransaction] = []
        declared_inflow: int | None = None
        declared_outflow: int | None = None
        in_table = False
        expect_totals = False

        for row in reader:
            if not row or not any(cell.strip() for cell in row):
                expect_totals = False  # a blank line ends the totals section
                continue

            first = row[0].strip()

            # Per-currency totals: the "#Waluta;#Wpływy;#Wydatki" header is
            # followed by a "PLN;<inflow>;<outflow>" row we read for reconciliation.
            if first == _TOTALS_HEADER_FIRST_CELL:
                expect_totals = True
                continue
            if expect_totals:
                expect_totals = False
                if len(row) >= 3:
                    declared_inflow = _parse_total(row[1])
                    declared_outflow = _parse_total(row[2])
                continue

            if first == _COLUMN_HEADER_FIRST_CELL:
                in_table = True
                continue

            # Only rows inside the transaction table whose first cell is an ISO
            # date are transactions; everything else is preamble/footer noise.
            if not in_table or not _ISO_DATE.match(first):
                continue
            if len(row) <= _AMOUNT:
                continue  # truncated row, missing the amount column

            # A malformed row fails the whole import (see ImporterError): silently
            # skipping it would drop a real transaction and under-count money.
            try:
                booked_date = date.fromisoformat(first)
                amount = parse_pln(row[_AMOUNT])
            except ValueError as exc:  # MoneyParseError is a ValueError too
                raise ImporterError(f"mBank CSV line {reader.line_num}: {exc}") from exc

            out.append(
                NormalizedTransaction(
                    booked_date=booked_date,
                    amount=amount,
                    raw_description=_clean(row[_DESCRIPTION]),
                    balance_after=None,  # mBank exports carry no running balance
                )
            )

        return ParseResult(
            transactions=out,
            declared_inflow=declared_inflow,
            declared_outflow=declared_outflow,
        )
