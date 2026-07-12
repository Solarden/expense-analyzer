"""Config / app-bootstrap guards."""

import pytest
from pydantic import ValidationError

from expense_analyzer.config import INSECURE_DEFAULT_SECRET, Settings, get_settings
from expense_analyzer.main import create_app


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


@pytest.mark.parametrize("base_url", ["", "   "])
def test_llm_enabled_requires_a_base_url(base_url):
    """Enabling the LLM with no base URL is a silent no-op (it would fall back to the
    classifier forever) — reject it loudly at startup instead."""
    with pytest.raises(ValidationError):
        Settings(llm_enabled=True, llm_base_url=base_url)


def test_llm_enabled_with_base_url_is_accepted():
    s = Settings(llm_enabled=True, llm_base_url="http://ollama:11434")
    assert s.llm_enabled is True
    assert s.llm_base_url == "http://ollama:11434"


def test_llm_disabled_needs_no_base_url():
    """The default (off) must construct fine with no URL."""
    assert Settings(llm_enabled=False).llm_enabled is False


def test_create_app_refuses_insecure_default_secret(monkeypatch):
    """Outside debug, the app must not boot with the public default secret."""
    monkeypatch.setenv("EA_SECRET_KEY", INSECURE_DEFAULT_SECRET)
    monkeypatch.setenv("EA_DEBUG", "false")
    get_settings.cache_clear()

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

    try:
        create_app()  # must not raise
    finally:
        get_settings.cache_clear()
