"""opt_int: lenient int coercion for optional query/form params."""

from expense_analyzer.api.params import opt_int


def test_opt_int_parses_valid_and_falls_back_on_junk():
    assert opt_int("5") == 5
    assert opt_int("0") == 0
    assert opt_int("999999") == 999999  # large ids don't overflow / crash

    assert opt_int(None) is None
    assert opt_int("") is None
    assert opt_int("abc") is None
    assert opt_int(" 5 ") is None  # embedded whitespace -> not a plain digit string


def test_opt_int_rejects_isdigit_true_but_unparseable():
    # str.isdigit() is True for these, but int() raises ValueError. The helper must
    # return None (fall back) rather than let the crash surface as an HTTP 500 —
    # the exact regression it exists to prevent.
    assert "²".isdigit() and opt_int("²") is None
    assert "³".isdigit() and opt_int("³") is None
