from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel default for secret_key. The app refuses to start with this value
# unless debug mode is on (see main.create_app) — never use it in production.
# Not a real secret (it's the *rejected* placeholder), so silence bandit B105.
INSECURE_DEFAULT_SECRET = "dev-insecure-secret-change-me"  # nosec B105


class Settings(BaseSettings):
    """Application settings, overridable via environment or a .env file."""

    model_config = SettingsConfigDict(env_prefix="EA_", env_file=".env", extra="ignore")

    app_name: str = "Expense Analyzer"
    debug: bool = False

    # Secret used to sign session cookies. MUST be overridden in production
    # (set EA_SECRET_KEY); changing it invalidates all logged-in sessions.
    secret_key: str = INSECURE_DEFAULT_SECRET

    # Mark the session cookie Secure (browser sends it only over HTTPS). True in
    # production (behind Caddy TLS); left False for local http dev and tests.
    secure_cookies: bool = False

    # Path to the SQLite database file. Mounted as a volume in docker.
    database_path: Path = Path("data/expense_analyzer.db")

    # Display / bucketing timezone. The app stores and computes everything in
    # UTC internally; this only governs how instants are presented and how they
    # are grouped into local days/months (e.g. monthly budgets). See clock.py.
    timezone: str = "Europe/Warsaw"

    # Max gap (in days) between the two legs of an internal transfer for them to
    # be paired. Polish interbank ELIXIR settles only on business days, so a long
    # weekend stacked with public holidays (majówka, Christmas/New Year) can push
    # the receiving leg to ~D+4/D+5. Default 5 covers that worst realistic case.
    # Widening is cheap: auto-link still requires mutual uniqueness, so a wider
    # window only yields more *manual* suggestions, never a wrong auto-link. See
    # transfers.py.
    transfer_window_days: int = 5

    # Where this app's source code lives. Shown as a "Source" link in the UI so
    # the app honours AGPL §13 (offer the corresponding source to everyone who
    # interacts with it over the network). If you run a *modified* version, point
    # this at your own published source. See LICENSING.md.
    source_url: str = "https://github.com/Solarden/expense-analyzer"

    # Plan-vs-reality matching for loans (Phase 5). A real installment is an
    # outflow on a non-loan account; we suggest it for a scheduled installment
    # when it falls within this many days of the due date (same ELIXIR / long-
    # weekend reasoning as transfer_window_days) and within the amount tolerance.
    loan_match_window_days: int = 5
    # Allowed gap between an actual payment and the *planned* installment, as a
    # percentage of the plan. A percentage rather than a fixed amount because
    # variable-rate and decreasing-installment payments drift every month. See
    # queries/loans.py. Must be >= 0.
    loan_match_amount_tolerance_pct: int = Field(default=5, ge=0)

    # Rows per page on the transaction list. Small by default — the working
    # surface is browsed, not bulk-scrolled, and a tight page keeps the Pi's
    # render cheap. Overridable via EA_PAGE_SIZE; must be >= 1 (0 would mean an
    # empty page and a zero-division in the pager).
    page_size: int = Field(default=50, ge=1)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value!r}") from exc
        return value

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
