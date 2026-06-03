"""Defensive numeric parsing for external sources (myFund / XTB)."""

from decimal import Decimal

import pytest

from expense_analyzer.money import MoneyParseError, parse_loose_amount, parse_loose_decimal


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("+7,00", 700),  # leading '+' on a gain
        ("632.56000", 63256),  # dot decimal (XTB raw cell)
        ("1 234,56", 123456),  # space thousands + comma decimal
        ("1\xa0234,56", 123456),  # non-breaking space thousands
        ("1,234.56", 123456),  # US style: comma thousands, dot decimal
        ("1.234,56", 123456),  # EU style: dot thousands, comma decimal
        ("-5,00", -500),  # negative
        ("100,00 zł", 10000),  # currency suffix
    ],
)
def test_parse_loose_amount_values(text: str, expected: int) -> None:
    assert parse_loose_amount(text) == expected


@pytest.mark.parametrize("blank", ["", "&nbsp;", "---", "—", "-", "   "])
def test_parse_loose_amount_blank_is_none(blank: str) -> None:
    assert parse_loose_amount(blank) is None


def test_parse_loose_amount_none() -> None:
    assert parse_loose_amount(None) is None


def test_parse_loose_amount_numeric_inputs() -> None:
    assert parse_loose_amount(1000) == 100000  # int = whole PLN
    assert parse_loose_amount(632.56) == 63256  # float via str, no binary noise


def test_parse_loose_amount_rejects_garbage_and_bool() -> None:
    with pytest.raises(MoneyParseError):
        parse_loose_amount("nonsense")
    with pytest.raises(MoneyParseError):
        parse_loose_amount(True)  # bool is an int subclass — rejected explicitly


def test_parse_loose_decimal_keeps_fraction() -> None:
    assert parse_loose_decimal("0.1980") == Decimal("0.1980")
    assert parse_loose_decimal("1 234,5") == Decimal("1234.5")
    assert parse_loose_decimal("&nbsp;") is None
    assert parse_loose_decimal(None) is None
