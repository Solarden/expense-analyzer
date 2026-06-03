from decimal import Decimal

import pytest

from expense_analyzer.money import (
    MoneyParseError,
    format_pln,
    from_minor_units,
    parse_pln,
    to_minor_units,
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
def test_to_minor_units(value, expected):
    assert to_minor_units(value) == expected


def test_to_minor_units_rounds_half_up():
    # 0.005 PLN -> 0.5 of a minor unit, rounds to 1
    assert to_minor_units(Decimal("0.005")) == 1


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


def test_roundtrip_minor_units_decimal():
    assert from_minor_units(123456) == Decimal("1234.56")
    assert from_minor_units(-1) == Decimal("-0.01")


def test_format_pln_groups_thousands_and_uses_comma():
    # Thousands grouped with a non-breaking space, comma decimal (Polish style).
    assert format_pln(-123456) == "-1\xa0234,56 zł"
    assert format_pln(100) == "1,00 zł"  # under 1000: no grouping
    assert format_pln(-28561571) == "-285\xa0615,71 zł"
    assert format_pln(0) == "0,00 zł"
