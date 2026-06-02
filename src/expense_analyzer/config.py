from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, overridable via environment or a .env file."""

    model_config = SettingsConfigDict(env_prefix="EA_", env_file=".env", extra="ignore")

    app_name: str = "Expense Analyzer"
    debug: bool = False

    # Path to the SQLite database file. Mounted as a volume in docker.
    database_path: Path = Path("data/expense_analyzer.db")

    # Display / bucketing timezone. The app stores and computes everything in
    # UTC internally; this only governs how instants are presented and how they
    # are grouped into local days/months (e.g. monthly budgets). See clock.py.
    timezone: str = "Europe/Warsaw"

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
