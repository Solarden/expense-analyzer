"""PKO BP parser tests.

Two layers, all on anonymized data (no real financials in the repo):
- inline ``_SAMPLE`` strings for focused edge cases (UTF-8 vs cp1250, malformed
  rows, empty input);
- committed cp1250 fixtures in ``tests/fixtures/pko/`` exercised as regressions:
  ``sample.csv`` (real-format happy path), ``edge_cases.csv`` (valid-but-tricky
  rows), ``broken.csv`` (a malformed row → ImporterError).
"""

from datetime import date

import pytest

from expense_analyzer.importers.base import ImporterError
from expense_analyzer.importers.pko import PKOCsvImporter

# Committed anonymized fixtures live in tests/fixtures/pko/ (cp1250). The
# `fixtures_dir` fixture (conftest) resolves their location.

# Synthetic PKO export. Encoded to cp1250 in the fixture below so the decoding
# path (Polish characters: ł, ó, ż, ą) is exercised.
_SAMPLE = (
    '"Data operacji","Data waluty","Typ transakcji","Kwota","Waluta",'
    '"Saldo po transakcji","Opis transakcji","","","","","",""\n'
    # Pending authorization hold: empty date, "W rozliczeniu" balance -> skipped.
    '"","","Blokada","-29.99","PLN","W rozliczeniu","Tytuł: P000  ",'
    '"Lokalizacja: Adres: TEST SHOP Miasto: Testowo Kraj: POLSKA","","","","",""\n'
    # Card payment (expense), trailing columns filled.
    '"2026-05-31","2026-05-31","Płatność kartą","-42.40","PLN","+325.76","Tytuł: 111 ",'
    '"Lokalizacja: Adres: Testowy Sklep Miasto: Łódź Kraj: POLSKA",'
    '"Data wykonania operacji: 2026-05-31","Oryginalna kwota operacji: 42.40",'
    '"Numer karty: 425125******0000","",""\n'
    # Incoming transfer (inflow).
    '"2026-05-30","2026-05-30","Przelew na konto","+1400.00","PLN","+1725.76",'
    '"Rachunek nadawcy: 00 0000 0000 0000 0000 0000 0000","Nazwa nadawcy: JAN KOWALSKI",'
    '"Tytuł: PRZELEW TESTOWY","","","",""\n'
    # Outgoing transfer (expense).
    '"2026-05-29","2026-05-29","Przelew z rachunku","-200.00","PLN","+1525.76",'
    '"Rachunek odbiorcy: 11 1111 1111 1111 1111 1111 1111","Nazwa odbiorcy: ANNA NOWAK",'
    '"Tytuł: CZYNSZ","","","",""\n'
    # Foreign-currency card payment: original amount and FX margin in trailing cols.
    '"2026-05-06","2026-05-07","Płatność kartą","-4.93","PLN","+1520.83","Tytuł: 222 ",'
    '"Lokalizacja: Adres: Kawiarnia Miasto: Praha Kraj: CZECHY",'
    '"Data wykonania operacji: 2026-05-06","Oryginalna kwota operacji: 27.00",'
    '"Marża za przewalutowanie: 5,02","Data przetworzenia: 2026-05-07",'
    '"Numer karty: 425125******0000"\n'
    "\n"  # trailing blank line
)


@pytest.fixture
def sample_bytes() -> bytes:
    return _SAMPLE.encode("cp1250")


def test_parses_only_booked_rows(sample_bytes: bytes):
    txns = PKOCsvImporter().parse(sample_bytes)
    # 5 data rows, but the pending "Blokada" is skipped -> 4.
    assert len(txns) == 4
    assert all(tx.amount != -2999 for tx in txns)  # the -29.99 hold is gone


def test_card_payment_fields(sample_bytes: bytes):
    card = PKOCsvImporter().parse(sample_bytes)[0]
    assert card.booked_date == date(2026, 5, 31)
    assert card.amount == -4240  # grosze, signed
    assert card.balance_after == 32576
    # cp1250 decoding + type prefix + joined fragments.
    assert card.raw_description.startswith("Płatność kartą | Tytuł: 111 |")
    assert "Łódź" in card.raw_description
    assert "Numer karty: 425125******0000" in card.raw_description
    assert "  " not in card.raw_description  # whitespace collapsed


def test_signs_and_amounts(sample_bytes: bytes):
    txns = PKOCsvImporter().parse(sample_bytes)
    by_amount = {tx.amount for tx in txns}
    assert 140000 in by_amount  # +1400.00 inflow stays positive
    assert -20000 in by_amount  # -200.00 expense stays negative


def test_fx_row_keeps_pln_amount_and_extra_fragments(sample_bytes: bytes):
    fx = PKOCsvImporter().parse(sample_bytes)[3]
    assert fx.amount == -493  # the PLN amount, not the 27.00 original
    assert fx.balance_after == 152083
    assert "Marża za przewalutowanie: 5,02" in fx.raw_description
    assert "CZECHY" in fx.raw_description


def test_empty_input_yields_nothing():
    assert PKOCsvImporter().parse(b"") == []


def test_utf8_file_decodes_polish_characters():
    # A genuine UTF-8 export must decode via the UTF-8 path, not get mojibaked.
    txns = PKOCsvImporter().parse(_SAMPLE.encode("utf-8"))
    assert len(txns) == 4
    assert "Łódź" in txns[0].raw_description


def test_malformed_amount_raises_importer_error():
    bad = (
        '"Data operacji","Data waluty","Typ transakcji","Kwota","Waluta",'
        '"Saldo po transakcji","Opis transakcji","","","","","",""\n'
        '"2026-05-31","2026-05-31","Płatność kartą","not-a-number","PLN","+325.76",'
        '"Tytuł: x","","","","","",""\n'
    ).encode("cp1250")
    with pytest.raises(ImporterError, match="line 2"):
        PKOCsvImporter().parse(bad)


def test_real_format_fixture_regression(fixtures_dir):
    """Regression against an anonymized real-format PKO export (cp1250)."""
    txns = PKOCsvImporter().parse((fixtures_dir / "pko" / "sample.csv").read_bytes())

    # 5 rows, the pending Blokada is skipped -> 4 booked, in file order.
    assert [(t.booked_date, t.amount, t.balance_after) for t in txns] == [
        (date(2026, 5, 31), -4240, 132576),  # card payment
        (date(2026, 5, 30), 150000, 136816),  # incoming transfer
        (date(2026, 5, 29), -20000, -13184),  # outgoing transfer
        (date(2026, 5, 6), -493, 6816),  # foreign-currency card payment
    ]

    card = txns[0]
    assert card.raw_description.startswith("Płatność kartą |")
    assert "Łódź" in card.raw_description  # cp1250 decoded correctly
    assert "Numer karty: 400000******0000" in card.raw_description

    fx = txns[3]
    assert "Marża za przewalutowanie: 5,02" in fx.raw_description
    assert "CZECHY" in fx.raw_description


def test_edge_cases_fixture(fixtures_dir):
    """Valid-but-tricky rows: zero/large amounts, refund, value-date != op-date,
    messy whitespace, a blank line in the middle."""
    txns = PKOCsvImporter().parse((fixtures_dir / "pko" / "edge_cases.csv").read_bytes())

    # Blank line skipped -> 5 rows, order preserved.
    assert [t.amount for t in txns] == [0, 2_000_000, 3499, -999, -150]

    # booked_date comes from the operation date (col 0), not the value date.
    assert txns[3].booked_date == date(2026, 4, 4)
    # Minimal description: just the type + Tytuł.
    assert txns[3].raw_description == "Płatność kartą | Tytuł: 3"
    # Whitespace inside fields is collapsed.
    assert "spaced title" in txns[4].raw_description
    assert "  " not in txns[4].raw_description


def test_broken_fixture_raises_with_line_number(fixtures_dir):
    """A malformed amount mid-file fails the whole import, citing the CSV line."""
    with pytest.raises(ImporterError, match="line 3"):
        PKOCsvImporter().parse((fixtures_dir / "pko" / "broken.csv").read_bytes())


def test_malformed_date_raises_importer_error():
    bad = (
        '"Data operacji","Data waluty","Typ transakcji","Kwota","Waluta",'
        '"Saldo po transakcji","Opis transakcji","","","","","",""\n'
        '"2026-13-99","2026-05-31","Płatność kartą","-1.00","PLN","+1.00",'
        '"Tytuł: x","","","","","",""\n'
    ).encode("cp1250")
    with pytest.raises(ImporterError):
        PKOCsvImporter().parse(bad)
