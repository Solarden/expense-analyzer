"""mBank CSV parser tests.

mBank differs from PKO in every surface detail (UTF-8-with-BOM vs cp1250,
semicolon vs comma, a long preamble, declared period totals instead of a running
balance), so it gets its own parser and its own tests. All data is anonymized;
committed fixtures live in ``tests/fixtures/mbank/``.
"""

from datetime import date

import pytest

from expense_analyzer.importers.base import ImporterError
from expense_analyzer.importers.mbank import MBankCsvImporter

# Minimal synthetic export covering the structural quirks: a multi-line preamble,
# the per-currency totals block, the column header, then two transactions.
_SAMPLE = (
    "mBank S.A. Bankowość Detaliczna;\r\n"
    "\t\tSkrytka Pocztowa 2108;\r\n"
    "\r\n"
    "#Klient;\r\n"
    "JAN KOWALSKI;\r\n"
    "\r\n"
    "Lista operacji;\r\n"
    "\r\n"
    "#Za okres:;\r\n"
    "01.01.2026;31.12.2026;\r\n"
    "\r\n"
    "      #Waluta;#Wpływy;#Wydatki;\r\n"
    "PLN;1 400,00;-242,40;\r\n"
    "      \r\n"
    "#Data operacji;#Opis operacji;#Rachunek;#Kategoria;#Kwota;\r\n"
    '2026-05-31;"BIEDRONKA 4521 ŁÓDŹ, Płatność kartą  400000******0000";'
    '"eKonto 0000 ... 0000";"Jedzenie";-242,40 PLN;;\r\n'
    '2026-05-30;"PRACODAWCA SP. Z O.O., WYNAGRODZENIE";'
    '"eKonto 0000 ... 0000";"Wpływy - inne";1 400,00 PLN;;\r\n'
    "\r\n"
)


@pytest.fixture
def sample_bytes() -> bytes:
    return _SAMPLE.encode("utf-8-sig")


def test_parses_transactions_skipping_preamble(sample_bytes: bytes):
    result = MBankCsvImporter().parse(sample_bytes)
    assert len(result.transactions) == 2
    assert [t.booked_date for t in result.transactions] == [date(2026, 5, 31), date(2026, 5, 30)]


def test_amounts_signed_and_pln_suffix_stripped(sample_bytes: bytes):
    result = MBankCsvImporter().parse(sample_bytes)
    assert result.transactions[0].amount == -24240  # "-242,40 PLN"
    assert result.transactions[1].amount == 140000  # "1 400,00 PLN"


def test_no_running_balance(sample_bytes: bytes):
    # mBank exports carry no per-row balance; reconciliation uses declared totals.
    assert all(t.balance_after is None for t in MBankCsvImporter().parse(sample_bytes).transactions)


def test_declared_totals_captured(sample_bytes: bytes):
    result = MBankCsvImporter().parse(sample_bytes)
    assert result.declared_inflow == 140000
    assert result.declared_outflow == -24240


def test_description_whitespace_collapsed(sample_bytes: bytes):
    desc = MBankCsvImporter().parse(sample_bytes).transactions[0].raw_description
    assert "BIEDRONKA 4521 ŁÓDŹ" in desc
    assert "  " not in desc  # the double space in the source is collapsed


def test_cp1250_fallback_decodes_polish_characters():
    # Older mBank exports were windows-1250; the UTF-8 path fails and we fall back.
    txns = MBankCsvImporter().parse(_SAMPLE.encode("cp1250")).transactions
    assert "ŁÓDŹ" in txns[0].raw_description


def test_empty_input_yields_nothing():
    result = MBankCsvImporter().parse(b"")
    assert result.transactions == []
    assert result.declared_inflow is None


def test_malformed_amount_raises_with_line_number():
    bad = (
        "#Data operacji;#Opis operacji;#Rachunek;#Kategoria;#Kwota;\r\n"
        '2026-05-31;"x";"eKonto 0000 ... 0000";"Inne";not-a-number PLN;;\r\n'
    ).encode("utf-8-sig")
    with pytest.raises(ImporterError, match="line 2"):
        MBankCsvImporter().parse(bad)


def test_real_format_fixture_regression(fixtures_dir):
    """Regression against an anonymized real-format mBank export (UTF-8 BOM)."""
    result = MBankCsvImporter().parse((fixtures_dir / "mbank" / "sample.csv").read_bytes())

    assert [(t.booked_date, t.amount) for t in result.transactions] == [
        (date(2026, 4, 13), -12345),
        (date(2026, 3, 31), 850000),
        (date(2026, 3, 15), -230000),
        (date(2026, 3, 10), 500),
    ]
    # Declared totals equal the sum of the rows -> reconciliation passes (see
    # test_reconciliation). Inflow 5,00 + 8500,00 = 8505,00; outflow likewise.
    assert result.declared_inflow == 850500
    assert result.declared_outflow == -242345


def test_edge_cases_fixture_tolerates_blank_line_mid_table(fixtures_dir):
    result = MBankCsvImporter().parse((fixtures_dir / "mbank" / "edge_cases.csv").read_bytes())
    # Zero, a large amount, a positive refund, a messy-whitespace expense — the
    # blank line in the middle of the table is skipped, not treated as the end.
    assert [t.amount for t in result.transactions] == [0, 123456789, 9999, -1230]


def test_broken_fixture_raises_with_line_number(fixtures_dir):
    with pytest.raises(ImporterError, match="line"):
        MBankCsvImporter().parse((fixtures_dir / "mbank" / "broken.csv").read_bytes())
