import os
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The app refuses to start with this value unless debug is on (main.create_app).
# Not a real secret — it is the *rejected* placeholder — so silence bandit B105.
INSECURE_DEFAULT_SECRET = "dev-insecure-secret-change-me"  # nosec B105


class Settings(BaseSettings):
    """Application settings, overridable via environment or a .env file."""

    model_config = SettingsConfigDict(env_prefix="EA_", env_file=".env", extra="ignore")

    def __init__(self, **kwargs: object) -> None:
        # Skips the local .env so tests never inherit a developer's real config
        # (Secure cookies alone would break the plain-http test client). Read at
        # construction time, so it also covers the alembic/backup subprocesses.
        if os.getenv("EA_NO_DOTENV") and "_env_file" not in kwargs:
            kwargs["_env_file"] = None
        super().__init__(**kwargs)

    app_name: str = "Expense Analyzer"
    debug: bool = False

    # Secret used to sign session cookies. MUST be overridden in production
    # (set EA_SECRET_KEY); changing it invalidates all logged-in sessions.
    secret_key: str = INSECURE_DEFAULT_SECRET

    # Mark the session cookie Secure (browser sends it only over HTTPS). True in
    # production (behind Caddy TLS); left False for local http dev and tests.
    secure_cookies: bool = False

    # Production points at a PostgreSQL server (see docker-compose.yml); the SQLite
    # default keeps local dev zero-setup. Engine behaviour is dialect-aware (db.py).
    database_url: str = "sqlite:///data/expense_analyzer.db"

    # The app's file-data directory (update_status.json, attachments). Independent
    # of the database, which may live on a separate server.
    data_path: Path = Path("data")

    # How many newest backups pruning keeps; 0 keeps all, --keep overrides.
    # deploy.sh reads EA_BACKUP_KEEP from .env itself (shell can't see pydantic).
    backup_keep: int = Field(default=14, ge=0)

    # --- Loan attachments ----------------------------------------------------
    # In docker, compose pins this to the mounted /data volume so files survive
    # rebuilds. On-disk names are generated UUIDs, so no filename can escape it.
    attachments_path: Path = Path("data/attachments")
    # A scanned multi-page contract PDF is the realistic worst case. Allowed file
    # *types* are a security boundary kept in code (attachments.py), not env.
    attachment_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    # Guards against an accidental mass upload filling the volume.
    attachment_max_per_loan: int = Field(default=50, ge=1)

    # Display and bucketing only — everything is stored and computed in UTC. Governs
    # how instants group into local days/months (e.g. monthly budgets). See clock.py.
    timezone: str = "Europe/Warsaw"

    # Max gap between an internal transfer's two legs. Polish interbank ELIXIR
    # settles only on business days, so a long weekend can push it to D+4/D+5.
    transfer_window_days: int = 5

    # Shown as a "Source" link so the app honours AGPL §13. Running a *modified*
    # version means pointing this at your own published source. See LICENSING.md.
    source_url: str = "https://github.com/Solarden/expense-analyzer"

    # A scheduled installment matches a real outflow within this many days of its
    # due date (same ELIXIR reasoning as transfer_window_days).
    loan_match_window_days: int = 5
    # A percentage rather than a fixed amount, because variable-rate and
    # decreasing-installment payments drift every month.
    loan_match_amount_tolerance_pct: int = Field(default=5, ge=0)

    # Small by default: the list is browsed, not bulk-scrolled. Must stay >= 1 —
    # 0 would mean an empty page and a zero-division in the pager.
    page_size: int = Field(default=50, ge=1)

    # --- myFund.pl investment import -----------------------------------------
    # OPT-IN, off by default: with no API key the app makes zero outbound calls.
    # A key plus portfolio name turns on the app's only network egress (read-only).
    myfund_api_base_url: str = "https://myfund.pl/API/v1"
    myfund_api_key: SecretStr = SecretStr("")  # masked in logs/repr
    myfund_portfolio: str = ""  # portfolio name as shown in the myFund account
    # Worker poll interval in hours. None/0 = the background worker never fetches
    # (the manual "Fetch now" button on the Investments page still works).
    myfund_fetch_interval_hours: int | None = Field(default=None)
    # Portfolio Account the worker imports myFund positions into. Required for the
    # *worker* path only (the UI picks the account per request). None = worker idle.
    myfund_account_id: int | None = Field(default=None)

    @field_validator("myfund_fetch_interval_hours", "myfund_account_id", mode="before")
    @classmethod
    def _blank_str_to_none(cls, value: object) -> object:
        # A blank env value is the natural way to leave an optional int unset, so
        # treat it as None rather than crashing on int parsing.
        if isinstance(value, str) and not value.strip():
            return None

        return value

    @property
    def myfund_configured(self) -> bool:
        """True once an API key and portfolio name are set — gates all egress."""
        return bool(self.myfund_api_key.get_secret_value() and self.myfund_portfolio)

    # --- Home Assistant via MQTT ---------------------------------------------
    # OPT-IN, off by default. Unlike myFund this is not internet egress: the broker
    # is on the LAN, and the push is one-directional and read-only.
    mqtt_host: str = ""  # empty -> MQTT disabled (gates everything)
    mqtt_port: int = Field(default=1883, ge=1, le=65535)
    mqtt_username: str = ""
    mqtt_password: SecretStr = SecretStr("")  # masked in logs/repr
    # Topic prefix for this app's own state/availability/alert topics.
    mqtt_base_topic: str = "expense_analyzer"
    # Where HA listens for MQTT discovery configs (HA's default is "homeassistant").
    mqtt_discovery_prefix: str = "homeassistant"
    # Worker auto-publish cadence in minutes. None/0 = the background worker never
    # pushes on its own (the manual "Publish now" button still works).
    mqtt_publish_interval_minutes: int | None = Field(default=None)

    @property
    def mqtt_configured(self) -> bool:
        """True once a broker host is set — gates every MQTT connection."""
        return bool(self.mqtt_host)

    # --- Subscription / recurring cost detection -----------------------------
    # Derived from existing transactions — no egress, no opt-in. These tune how
    # forgiving the detector is; override only if real data shows it misjudging.

    # Minimum charges from one merchant before it can be called recurring.
    subscription_min_occurrences: int = Field(default=3, ge=2)
    # Absorbs normal drift (FX, a small plan tweak) without admitting variable
    # spending at the same shop.
    subscription_amount_tolerance_pct: int = Field(default=15, ge=0)
    # Independent of the tolerance above: that absorbs drift, this flags a
    # deliberate price step worth pinging HA about.
    subscription_price_rise_pct: int = Field(default=10, ge=0)
    # A subscription whose first charge lands within this many days is "new" —
    # flagged in the UI and alerted to HA once (until you confirm it).
    subscription_new_window_days: int = Field(default=35, ge=1)

    # --- Categorization classifier (layer 2) ---------------------------------
    # TF-IDF + logistic regression, trained fresh on each run from confirmed
    # categorizations. No stored model, no egress.

    # At or above this confidence the category auto-applies; below it the row waits
    # in the review queue. A probabilistic guess on financial data errs toward the queue.
    classifier_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    # Below this many confirmed labels the model is noise, so training is a no-op.
    # Logistic regression also needs >= 2 distinct categories.
    classifier_min_training_samples: int = Field(default=25, ge=2)

    # --- Categorization embeddings (layer 3) ---------------------------------
    # Embeds each transaction and finds the nearest already-categorized one (kNN).
    # A suggestion only, never auto-applied. The model is baked into the image.

    # Fail-safe: if the model can't be loaded the queue simply shows no layer-3
    # hints. Set False to skip the heavy path entirely.
    embeddings_enabled: bool = True
    # Multilingual (Polish bank descriptions) and small enough for CPU inference.
    # Bundled in the image, so changing it means re-bundling.
    embeddings_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    # With HF_HUB_OFFLINE a commit-hash download has no "main" ref, so the offline
    # load must ask for the same revision it was bundled with. Change in lockstep
    # with embeddings_model — a revision belongs to one repo.
    embeddings_model_revision: str = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
    # How many nearest labelled neighbours vote on the category for a queued row.
    embeddings_neighbors: int = Field(default=5, ge=1)
    # Below this cosine similarity a neighbour is too far to be a useful hint, so
    # the queue stays quiet on merchants resembling nothing you've tagged.
    embeddings_min_similarity: float = Field(default=0.45, ge=0.0, le=1.0)
    # Same cold-start rationale as the classifier; also needs >= 2 categories.
    embeddings_min_training_samples: int = Field(default=25, ge=2)

    # --- LLM categorization (Ollama host; primary, local pipeline is fallback) ---
    # When enabled the LLM is the *primary* categorizer for "classify now", with the
    # sklearn classifier as fallback. LAN-only, like MQTT — not internet egress.

    # OPT-IN, off by default: with the feature off the client is never constructed.
    llm_enabled: bool = False
    # The Ollama host's base URL, e.g. "http://192.168.1.x:11434". Required when
    # llm_enabled is True (see the validator below).
    llm_base_url: str = ""
    # The model tag pulled on the Ollama host. A small "utility" model is the cheap
    # fit for classification.
    llm_model: str = "gemma3:12b"
    # Generous, because inference is slow. A short connect timeout in ollama.py
    # makes a *down* host fail fast rather than wait this long.
    llm_timeout: int = Field(default=30, ge=1)
    # Same conservative default as the classifier: below this the row stays in the
    # review queue rather than being auto-categorized.
    llm_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _require_base_url_when_llm_enabled(self) -> "Settings":
        # Without this the categorizer falls back to the classifier forever, which
        # looks like "the LLM isn't working". Fail loud at startup instead.
        if self.llm_enabled and not self.llm_base_url.strip():
            raise ValueError(
                "EA_LLM_ENABLED is set but EA_LLM_BASE_URL is empty — "
                "set the Ollama host URL, or disable the LLM."
            )

        return self

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value!r}") from exc

        return value

    @property
    def update_status_path(self) -> Path:
        """Where the cron update check writes its verdict for the Updates view.

        The web app only ever reads it; the network egress stays on the host's cron.
        """
        return self.data_path / "update_status.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
