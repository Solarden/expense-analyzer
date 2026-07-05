"""Config / app-bootstrap guards."""

import pytest
from pydantic import ValidationError

from expense_analyzer.config import INSECURE_DEFAULT_SECRET, Settings, get_settings


def test_page_size_must_be_positive():
    """page_size 0 would mean an empty page and a zero-division in the pager."""
    with pytest.raises(ValidationError):
        Settings(page_size=0)


@pytest.mark.parametrize("field", ["myfund_account_id", "myfund_fetch_interval_hours"])
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", None),  # blank env value ("leave it unset") -> None, not a crash
        ("   ", None),  # whitespace-only -> None
        ("7", 7),  # a real integer string (how env vars arrive) still coerces
    ],
)
def test_optional_int_blank_becomes_none(field, raw, expected):
    assert getattr(Settings(**{field: raw}), field) == expected


def test_create_app_refuses_insecure_default_secret(monkeypatch):
    """Outside debug, the app must not boot with the public default secret."""
    monkeypatch.setenv("EA_SECRET_KEY", INSECURE_DEFAULT_SECRET)
    monkeypatch.setenv("EA_DEBUG", "false")
    get_settings.cache_clear()

    from expense_analyzer.main import create_app

    try:
        with pytest.raises(RuntimeError, match="EA_SECRET_KEY"):
            create_app()
    finally:
        get_settings.cache_clear()  # restore real settings for other tests


def test_debug_mode_allows_default_secret(monkeypatch):
    """Debug (e.g. `make dev`) may use the insecure default for convenience."""
    monkeypatch.setenv("EA_SECRET_KEY", INSECURE_DEFAULT_SECRET)
    monkeypatch.setenv("EA_DEBUG", "true")
    get_settings.cache_clear()

    from expense_analyzer.main import create_app

    try:
        create_app()  # must not raise
    finally:
        get_settings.cache_clear()
