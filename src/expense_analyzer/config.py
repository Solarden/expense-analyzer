from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
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
    # be paired. Polish interbank transfers usually book D+1/D+2; widen this only
    # if real exports show slower settlement. See transfers.py.
    transfer_window_days: int = 3

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
