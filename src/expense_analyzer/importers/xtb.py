"""XTB investment export parser (design §7.3) — the offline positions source.

XTB exports an account as an **.xlsx** workbook (not CSV) with four sheets:
``CLOSED POSITION HISTORY``, ``OPEN POSITION <DDMMYYYY>``, ``PENDING ORDERS
HISTORY`` and ``CASH OPERATION HISTORY``. We read only ``OPEN POSITION`` — the
snapshot of current holdings.

Parsing is **stdlib only** (``zipfile`` + ``xml.etree``): an .xlsx is a zip of
XML. This keeps a zero-dependency posture (no openpyxl) and — the reason that
matters here — lets us read each cell's *raw* string and feed it straight to
:class:`~decimal.Decimal`, so money never passes through a binary float.

Layout of the OPEN POSITION sheet:
- A header block near the top with labels ``Balance`` / ``Equity`` (cash and total
  account value) sitting one row above their values.
- A positions table whose header row contains ``Position | Symbol | … | Volume |
  … | Market price | Purchase value | … | Gross P/L | …``, one row **per lot**
  (the same symbol can appear several times at different open prices).

We aggregate lots per symbol. A holding's current market value is taken as
``Purchase value + Gross P/L`` (cost basis plus profit/loss) rather than
``quantity × price`` — that matches XTB's own ``Equity`` and sidesteps any
price/quantity rounding or scaling. The snapshot date comes from the sheet name.
"""

import io
import re
import zipfile
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from xml.etree import ElementTree as ET  # nosec B405 — trusted single-user upload; see _parse_xml

from expense_analyzer.importers.base import ImporterError
from expense_analyzer.importers.positions import NormalizedPosition, PositionsResult
from expense_analyzer.money import parse_loose_amount, parse_loose_decimal

# OOXML spreadsheet namespace (cells, rows) and the relationships namespace.
_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Header cells we locate by name (XTB may shift columns / prepend a blank one).
_COL_SYMBOL = "Symbol"
_COL_VOLUME = "Volume"
_COL_MARKET_PRICE = "Market price"
_COL_PURCHASE_VALUE = "Purchase value"
_COL_GROSS_PL = "Gross P/L"

# Size guards (defense-in-depth against a malicious/corrupt upload). A real XTB
# IKE export is a few KB to well under 1 MB even with embedded logos, and the
# largest worksheet XML is a fraction of a MB — so these caps leave a huge margin
# while bounding memory and refusing a zip bomb (which expands to GBs):
#   * MAX_XLSX_BYTES      — the raw uploaded file (also enforced at the endpoint).
#   * MAX_PART_BYTES      — any single *uncompressed* zip member we read, checked
#                           against the zip directory before decompressing it.
MAX_XLSX_BYTES = 10 * 1024 * 1024  # 10 MiB
MAX_PART_BYTES = 50 * 1024 * 1024  # 50 MiB


class XTBImporter:
    """Parses an XTB ``.xlsx`` export into a :class:`PositionsResult`."""

    source = "XTB xlsx"

    def parse(self, data: bytes) -> PositionsResult:
        if len(data) > MAX_XLSX_BYTES:
            raise ImporterError(
                f"file is too large ({len(data)} bytes); an XTB export is well under "
                f"{MAX_XLSX_BYTES // (1024 * 1024)} MiB"
            )
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ImporterError("not a valid .xlsx file (bad zip)") from exc

        try:
            sheet_name, sheet_path = self._find_open_position_sheet(zf)
            rows = self._read_sheet_rows(zf, sheet_path)
        except KeyError as exc:
            raise ImporterError(f"unexpected .xlsx structure: missing {exc}") from exc

        snapshot = _snapshot_date_from_name(sheet_name)
        positions = self._extract_positions(rows, snapshot)
        cash_balance = _labelled_value(rows, "Balance")
        equity = _labelled_value(rows, "Equity")

        return PositionsResult(
            positions=positions,
            declared_total=equity,
            cash_balance=cash_balance,
        )

    # -- workbook navigation -------------------------------------------------

    def _find_open_position_sheet(self, zf: zipfile.ZipFile) -> tuple[str, str]:
        """Return ``(sheet_name, worksheet_path)`` for the OPEN POSITION sheet."""
        workbook = _parse_xml(_read_part(zf, "xl/workbook.xml"))
        rels = _parse_xml(_read_part(zf, "xl/_rels/workbook.xml.rels"))
        target_by_id = {
            r.get("Id"): r.get("Target") for r in rels.findall(f"{_NS_PKG_REL}Relationship")
        }
        for sheet in workbook.iter(f"{_NS_MAIN}sheet"):
            name = sheet.get("name") or ""
            if name.strip().upper().startswith("OPEN POSITION"):
                rid = sheet.get(f"{_NS_REL}id")
                target = target_by_id.get(rid)
                if target is None:
                    break
                return name, f"xl/{target.lstrip('/')}"

        raise ImporterError("no 'OPEN POSITION' sheet found — is this an XTB account export?")

    def _read_sheet_rows(self, zf: zipfile.ZipFile, path: str) -> list[list[str]]:
        """Read a worksheet into a list of rows, each a list of cell strings.

        Cells are placed by their column letter so a blank leading column or gaps
        keep their position. Handles inline strings, shared strings and raw values.
        """
        shared = _read_shared_strings(zf)
        root = _parse_xml(_read_part(zf, path))
        sheet_data = root.find(f"{_NS_MAIN}sheetData")
        if sheet_data is None:
            return []

        rows: list[list[str]] = []
        for row_el in sheet_data.findall(f"{_NS_MAIN}row"):
            cells: dict[int, str] = {}
            for cell in row_el.findall(f"{_NS_MAIN}c"):
                cells[_col_index(cell.get("r", "A1"))] = _cell_text(cell, shared)
            width = max(cells) + 1 if cells else 0
            rows.append([cells.get(i, "") for i in range(width)])

        return rows

    # -- positions table -----------------------------------------------------

    def _extract_positions(self, rows: list[list[str]], snapshot: date) -> list[NormalizedPosition]:
        header_idx, columns = _find_table_header(rows)
        if header_idx is None:
            raise ImporterError("OPEN POSITION sheet has no positions table header")

        # Aggregate lots per symbol: quantity, cost basis and market value sum.
        quantity: dict[str, Decimal] = defaultdict(Decimal)
        cost: dict[str, int] = defaultdict(int)
        market_value: dict[str, int] = defaultdict(int)
        last_price: dict[str, int | None] = {}
        order: list[str] = []

        for line_no, row in enumerate(rows[header_idx + 1 :], start=header_idx + 2):
            symbol = _cell(row, columns, _COL_SYMBOL).strip()
            if not symbol:
                break  # first blank row ends the table (a totals row, if any, is skipped)

            try:
                vol = parse_loose_decimal(_cell(row, columns, _COL_VOLUME))
                purchase = parse_loose_amount(_cell(row, columns, _COL_PURCHASE_VALUE))
                pl = parse_loose_amount(_cell(row, columns, _COL_GROSS_PL)) or 0
                price = parse_loose_amount(_cell(row, columns, _COL_MARKET_PRICE))
            except (InvalidOperation, ValueError) as exc:
                raise ImporterError(f"row {line_no}: cannot parse a number ({exc})") from exc
            if vol is None or purchase is None:
                raise ImporterError(f"row {line_no}: missing volume or purchase value for {symbol}")

            if symbol not in quantity:
                order.append(symbol)
            quantity[symbol] += vol
            cost[symbol] += purchase
            market_value[symbol] += purchase + pl  # current value = cost + P/L
            last_price[symbol] = price  # same across a symbol's lots; keep the last seen

        positions: list[NormalizedPosition] = []
        for symbol in order:
            qty = quantity[symbol]
            # Average purchase price per unit, rounded half-up to minor units to
            # match the project's money policy (never truncate toward zero).
            avg_price = (
                int((Decimal(cost[symbol]) / qty).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                if qty
                else None
            )
            positions.append(
                NormalizedPosition(
                    ticker=symbol,
                    quantity=qty,
                    value=market_value[symbol],
                    snapshot_date=snapshot,
                    avg_price=avg_price,
                    current_price=last_price.get(symbol),
                )
            )

        return positions


def _read_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """Shared-strings table if present (some exports use it instead of inline)."""
    try:
        raw = _read_part(zf, "xl/sharedStrings.xml")
    except KeyError:
        return []
    root = _parse_xml(raw)

    return ["".join(t.text or "" for t in si.iter(f"{_NS_MAIN}t")) for si in root]


def _read_part(zf: zipfile.ZipFile, name: str) -> bytes:
    """Read one zip member, refusing an implausibly large *uncompressed* size.

    The uncompressed size is taken from the zip directory (``ZipInfo.file_size``)
    and checked **before** ``read()`` decompresses anything, so a zip bomb (a tiny
    compressed member that expands to gigabytes) is rejected without being
    expanded into memory. Raises ``KeyError`` if the member is absent (callers that
    treat a part as optional, e.g. sharedStrings, catch it)."""
    info = zf.getinfo(name)  # KeyError if missing
    if info.file_size > MAX_PART_BYTES:
        raise ImporterError(
            f"{name} is implausibly large ({info.file_size} bytes uncompressed) "
            "— refusing to parse (possible zip bomb)"
        )

    return zf.read(name)


def _parse_xml(raw: bytes) -> ET.Element:
    """Parse an .xlsx XML part. Single point so the threat model is stated once.

    The workbook is uploaded by the authenticated household user — single-user,
    LAN-only, never publicly exposed (design §1/§10) — so the XML is not
    attacker-controlled in this app's threat model. stdlib ElementTree (no new
    dependency, in keeping with the project's minimalism) does not resolve
    external entities; that plus the trust boundary is why B314 is suppressed here.
    """
    return ET.fromstring(raw)  # nosec B314 — see docstring (trusted, single-user upload)


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    """Text of one ``<c>`` cell, resolving inline / shared strings and raw values."""
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        is_el = cell.find(f"{_NS_MAIN}is")
        return (
            "".join(t.text or "" for t in is_el.iter(f"{_NS_MAIN}t")) if is_el is not None else ""
        )
    value = cell.find(f"{_NS_MAIN}v")
    text = value.text or "" if value is not None else ""
    if cell_type == "s":  # shared-string index
        try:
            return shared[int(text)]
        except (ValueError, IndexError):
            return ""

    return text


def _col_index(ref: str) -> int:
    """Zero-based column index from a cell ref like ``"C12"`` -> ``2``."""
    letters = re.match(r"[A-Z]+", ref)
    n = 0
    for ch in letters.group(0) if letters else "A":
        n = n * 26 + (ord(ch) - ord("A") + 1)

    return n - 1


def _find_table_header(rows: list[list[str]]) -> tuple[int | None, dict[str, int]]:
    """Locate the positions table header row, mapping column name -> column index."""
    for idx, row in enumerate(rows):
        labels = {c.strip() for c in row}
        if "Position" in labels and _COL_SYMBOL in labels:
            columns = {c.strip(): i for i, c in enumerate(row) if c.strip()}
            return idx, columns

    return None, {}


def _cell(row: list[str], columns: dict[str, int], name: str) -> str:
    """Value of column ``name`` in ``row`` (empty string if absent)."""
    idx = columns.get(name)
    if idx is None or idx >= len(row):
        return ""

    return row[idx]


def _labelled_value(rows: list[list[str]], label: str) -> int | None:
    """Value below a header-block label (e.g. ``Equity``), in the same column.

    XTB prints the label and its value in adjacent rows, aligned by column.
    """
    for idx, row in enumerate(rows[:-1]):
        for col, text in enumerate(row):
            if text.strip() == label:
                below = rows[idx + 1]
                raw = below[col] if col < len(below) else ""
                return parse_loose_amount(raw)

    return None


def _snapshot_date_from_name(sheet_name: str) -> date:
    """Parse ``OPEN POSITION 15042026`` (DDMMYYYY) into a :class:`date`."""
    match = re.search(r"(\d{2})(\d{2})(\d{4})", sheet_name)
    if not match:
        raise ImporterError(f"could not read snapshot date from sheet name {sheet_name!r}")
    day, month, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ImporterError(f"invalid snapshot date in sheet name {sheet_name!r}") from exc
