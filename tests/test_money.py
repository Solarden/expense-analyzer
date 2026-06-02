from decimal import Decimal

import pytest

from expense_analyzer.money import (
    MoneyParseError,
    format_pln,
    from_grosze,
    parse_pln,
    to_grosze,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("123.45", 12345),
        ("100", 10000),
        ("0", 0),
        ("0.01", 1),
        (Decimal("99.99"), 9999),
        (5, 500),
    ],
)
def test_to_grosze(value, expected):
    assert to_grosze(value) == expected


def test_to_grosze_rounds_half_up():
    # 0.005 zł -> 0.5 grosza, rounds to 1
    assert to_grosze(Decimal("0.005")) == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1 234,56 zł", 123456),
        ("-1 234,56 zł", -123456),
        ("1\xa0234,56", 123456),  # non-breaking space thousands separator
        ("123,45 PLN", 12345),
        ("-50,00", -5000),
        ("  12,30  ", 1230),
        ("+7,00", 700),
    ],
)
def test_parse_pln(text, expected):
    assert parse_pln(text) == expected


@pytest.mark.parametrize("bad", ["", "   ", "abc", "zł"])
def test_parse_pln_rejects_garbage(bad):
    with pytest.raises(MoneyParseError):
        parse_pln(bad)


def test_roundtrip_grosze_decimal():
    assert from_grosze(123456) == Decimal("1234.56")
    assert from_grosze(-1) == Decimal("-0.01")


def test_format_pln_uses_comma():
    assert format_pln(-123456) == "-1234,56 zł"
    assert format_pln(100) == "1,00 zł"
