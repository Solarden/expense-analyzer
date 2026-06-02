"""PKO BP parser tests.

The fixture is **synthetic** (made-up amounts, names and accounts) but mirrors
the real export's structure exactly: windows-1250 encoding, comma-quoted fields,
the PKO column layout, a pending "Blokada" row, and a currency-conversion row
that fills the trailing description columns. No real financial data lives here.
"""

from datetime import date

import pytest

from expense_analyzer.importers.base import ImporterError
from expense_analyzer.importers.pko import PKOCsvImporter

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


def test_malformed_date_raises_importer_error():
    bad = (
        '"Data operacji","Data waluty","Typ transakcji","Kwota","Waluta",'
        '"Saldo po transakcji","Opis transakcji","","","","","",""\n'
        '"2026-13-99","2026-05-31","Płatność kartą","-1.00","PLN","+1.00",'
        '"Tytuł: x","","","","","",""\n'
    ).encode("cp1250")
    with pytest.raises(ImporterError):
        PKOCsvImporter().parse(bad)
