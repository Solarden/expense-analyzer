"""Config / app-bootstrap guards."""

import pytest

from expense_analyzer.config import INSECURE_DEFAULT_SECRET, get_settings


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
