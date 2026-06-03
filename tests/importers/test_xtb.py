"""XTB .xlsx parser tests.

The fixture .xlsx is built **programmatically** (the ``xtb_xlsx`` conftest fixture,
stdlib zip + XML) rather than committed as a binary — it doubles as documentation
of the layout the parser expects, and keeps real (non-anonymized) exports out of
the repo entirely.
"""

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from expense_analyzer.importers.base import ImporterError
from expense_analyzer.importers.xtb import XTBImporter


def test_parses_and_aggregates_lots_per_symbol(xtb_xlsx: Callable[..., bytes]) -> None:
    result = XTBImporter().parse(xtb_xlsx())

    by_ticker = {p.ticker: p for p in result.positions}
    assert set(by_ticker) == {"SXR8.DE", "SNT.PL"}

    sxr8 = by_ticker["SXR8.DE"]
    assert sxr8.quantity == Decimal("2")
    # value = (565.78+66.78) + (595.72+36.84) = 632.56 + 632.56 = 1265.12
    assert sxr8.value == 126512
    # avg price = cost / qty = (565.78 + 595.72) / 2 = 580.75
    assert sxr8.avg_price == 58075
    assert sxr8.current_price == 63256

    snt = by_ticker["SNT.PL"]
    assert snt.quantity == Decimal("3")
    assert snt.value == 90000  # 777.00 + 123.00
    assert snt.avg_price == 25900  # 777 / 3
    assert snt.current_price == 30000


def test_reads_snapshot_date_from_sheet_name(xtb_xlsx: Callable[..., bytes]) -> None:
    result = XTBImporter().parse(xtb_xlsx(sheet_name="OPEN POSITION 15042026"))

    assert all(p.snapshot_date == date(2026, 4, 15) for p in result.positions)


def test_reconciliation_inputs_balance_and_equity(xtb_xlsx: Callable[..., bytes]) -> None:
    result = XTBImporter().parse(xtb_xlsx())

    assert result.cash_balance == 1657
    assert result.declared_total == 218169  # equity
    assert result.cash_balance + sum(p.value for p in result.positions) == result.declared_total


def test_blank_row_ends_the_table(xtb_xlsx: Callable[..., bytes]) -> None:
    lots = [
        {
            "symbol": "SNT.PL",
            "volume": "3",
            "market": "300.00",
            "purchase": "777.00",
            "pl": "123.00",
        }
    ]
    result = XTBImporter().parse(xtb_xlsx(lots=lots))

    assert len(result.positions) == 1


def test_bad_number_raises_importer_error(xtb_xlsx: Callable[..., bytes]) -> None:
    lots = [
        {
            "symbol": "SNT.PL",
            "volume": "3",
            "market": "300.00",
            "purchase": "not-a-number",
            "pl": "0",
        }
    ]
    with pytest.raises(ImporterError):
        XTBImporter().parse(xtb_xlsx(lots=lots))


def test_not_a_zip_raises_importer_error() -> None:
    with pytest.raises(ImporterError):
        XTBImporter().parse(b"this is not an xlsx")


def test_missing_open_position_sheet_raises(xtb_xlsx: Callable[..., bytes]) -> None:
    with pytest.raises(ImporterError):
        XTBImporter().parse(xtb_xlsx(sheet_name="CASH OPERATION HISTORY"))


# --- Committed regression fixtures ------------------------------------------
# These read the on-disk .xlsx files in tests/fixtures/xtb/ (anonymized, real
# layout — see that dir's README). They guard the parser against the actual byte
# structure XTB produces, not just our in-memory builder's idea of it.


def test_sample_fixture_regression(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "xtb" / "sample.xlsx").read_bytes()
    result = XTBImporter().parse(data)

    by_ticker = {p.ticker: p for p in result.positions}
    assert set(by_ticker) == {"SXR8.DE", "SNT.PL"}
    assert by_ticker["SXR8.DE"].value == 126512
    assert by_ticker["SNT.PL"].value == 90000
    assert all(p.snapshot_date == date(2026, 4, 15) for p in result.positions)
    # Header block read correctly, and cash + Σ value reconciles to Equity.
    assert result.cash_balance == 1657
    assert result.declared_total == 218169
    assert result.cash_balance + sum(p.value for p in result.positions) == result.declared_total


def test_edge_cases_fixture(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "xtb" / "edge_cases.xlsx").read_bytes()
    result = XTBImporter().parse(data)

    by_ticker = {p.ticker: p for p in result.positions}
    # Fractional quantity is preserved exactly (Decimal, not float).
    assert by_ticker["SXR8.DE"].quantity == Decimal("0.1980")
    # Value = cost + Gross P/L, even when the P/L is a loss (negative).
    assert by_ticker["SXR8.DE"].value == 10702  # 112.02 + (-5.00)
    assert by_ticker["SNT.PL"].value == 90000
    assert result.cash_balance + sum(p.value for p in result.positions) == result.declared_total


def test_broken_fixture_raises(fixtures_dir: Path) -> None:
    data = (fixtures_dir / "xtb" / "broken.xlsx").read_bytes()
    with pytest.raises(ImporterError):
        XTBImporter().parse(data)


# --- Size guards (defense-in-depth) -----------------------------------------


def test_oversized_upload_rejected() -> None:
    from expense_analyzer.importers.xtb import MAX_XLSX_BYTES

    with pytest.raises(ImporterError, match="too large"):
        XTBImporter().parse(b"x" * (MAX_XLSX_BYTES + 1))


def test_zip_bomb_part_rejected(
    monkeypatch: pytest.MonkeyPatch, xtb_xlsx: Callable[..., bytes]
) -> None:
    # Shrink the per-part cap so any real member trips it — exercises the
    # ZipInfo.file_size check in _read_part without building a real bomb.
    import expense_analyzer.importers.xtb as xtb_mod

    monkeypatch.setattr(xtb_mod, "MAX_PART_BYTES", 10)
    with pytest.raises(ImporterError, match="zip bomb"):
        XTBImporter().parse(xtb_xlsx())
