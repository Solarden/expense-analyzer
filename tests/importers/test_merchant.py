"""Merchant normalization tests.

The normalizer is best-effort and shared across banks, so these assert the
stable, useful behaviours (markers extracted, noise stripped, casing/whitespace
normalized) rather than pinning the exact address tail, which is allowed to be
imperfect (see importers/merchant.py).
"""

from expense_analyzer.importers.merchant import normalize_merchant


def test_pko_card_payment_uses_address_field():
    raw = "Płatność kartą | Lokalizacja: Adres: Testowy Sklep Miasto: Łódź Kraj: POLSKA"
    assert normalize_merchant(raw) == "TESTOWY SKLEP"


def test_pko_transfer_uses_counterparty_name():
    raw = "Przelew na konto | Nazwa nadawcy: JAN KOWALSKI | Tytuł: PRZELEW"
    assert normalize_merchant(raw) == "JAN KOWALSKI"


def test_mbank_takes_head_before_first_comma():
    raw = "BIEDRONKA 4521 ŁÓDŹ, Płatność kartą 400000******0000"
    out = normalize_merchant(raw)
    assert out is not None
    assert out.startswith("BIEDRONKA 4521 ŁÓDŹ")


def test_card_number_and_postal_code_stripped():
    raw = "SKLEP 90-451, karta 400000******0000"
    out = normalize_merchant(raw)
    assert "400000" not in out
    assert "90-451" not in out
    assert out.startswith("SKLEP")


def test_uppercased_and_whitespace_collapsed():
    assert normalize_merchant("  small    shop  ") == "SMALL SHOP"


def test_blank_and_noise_only_return_none():
    assert normalize_merchant("") is None
    assert normalize_merchant("   ") is None
    assert normalize_merchant("400000******0000") is None
