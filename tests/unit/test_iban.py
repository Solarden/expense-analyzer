"""IBAN normalisation + mod-97 checksum (see expense_analyzer.iban)."""

import pytest

from expense_analyzer import iban

# A well-known valid Polish IBAN test value.
VALID = "PL61109010140000071219812874"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  pl61 1090 1014 0000 0712 1981 2874 ", VALID),  # spaces + lower-case
        (VALID, VALID),  # already canonical -> unchanged
    ],
)
def test_normalize(raw, expected):
    assert iban.normalize(raw) == expected


@pytest.mark.parametrize(
    "value, shaped",
    [
        (VALID, True),
        ("00-1234-5678", False),  # brokerage/cash id, not IBAN-shaped
        ("", False),
    ],
)
def test_looks_like_iban(value, shaped):
    assert iban.looks_like_iban(value) is shaped


@pytest.mark.parametrize(
    "value, valid",
    [
        (VALID, True),
        ("PL00109010140000071219812874", False),  # same shape, wrong check digits
    ],
)
def test_is_valid(value, valid):
    assert iban.is_valid(value) is valid
