"""IBAN normalisation + mod-97 checksum (see expense_analyzer.iban)."""

from expense_analyzer import iban

# A well-known valid Polish IBAN test value.
VALID = "PL61109010140000071219812874"


def test_normalize_strips_whitespace_and_uppercases():
    assert iban.normalize("  pl61 1090 1014 0000 0712 1981 2874 ") == VALID


def test_looks_like_iban():
    assert iban.looks_like_iban(VALID)
    assert not iban.looks_like_iban("00-1234-5678")  # brokerage/cash id, not IBAN-shaped
    assert not iban.looks_like_iban("")


def test_valid_iban_passes_checksum():
    assert iban.is_valid(VALID)


def test_bad_checksum_fails():
    # Same length/shape but wrong check digits (61 -> 00).
    assert not iban.is_valid("PL00109010140000071219812874")
