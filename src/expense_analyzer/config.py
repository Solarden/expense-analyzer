import os
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Sentinel default for secret_key. The app refuses to start with this value
# unless debug mode is on (see main.create_app) — never use it in production.
# Not a real secret (it's the *rejected* placeholder), so silence bandit B105.
INSECURE_DEFAULT_SECRET = "dev-insecure-secret-change-me"  # nosec B105


class Settings(BaseSettings):
    """Application settings, overridable via environment or a .env file."""

    model_config = SettingsConfigDict(env_prefix="EA_", env_file=".env", extra="ignore")

    def __init__(self, **kwargs: object) -> None:
        # EA_NO_DOTENV=1 skips the local .env so the test suite stays hermetic: it
        # must never inherit a developer's real config (which could force Secure
        # cookies and break the plain-http test client, or leave typed fields blank).
        # Read at construction time, so it covers every Settings() — direct or via
        # get_settings — and the alembic/backup subprocesses, which inherit the var.
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

    # Full SQLAlchemy database URL. Production points at the shared PostgreSQL
    # server (postgresql+psycopg://...; see docker-compose.yml); the SQLite
    # default keeps quick local dev zero-setup. Engine behavior is dialect-aware
    # (see db.py).
    database_url: str = "sqlite:///data/expense_analyzer.db"

    # The app's file-data directory (update_status.json; attachments default
    # below also lives here). Independent of the database now that the DB can be
    # a server rather than a file on this volume.
    data_path: Path = Path("data")

    # How many newest backups `python -m expense_analyzer.backup` keeps when
    # pruning (the design §10 cron path / `make backup`); 0 keeps all, an
    # explicit --keep on the CLI overrides. deploy.sh reads the same
    # EA_BACKUP_KEEP from .env on its own (shell scripts don't see pydantic).
    backup_keep: int = Field(default=14, ge=0)

    # --- Loan attachments (Phase 21) -----------------------------------------
    # Where uploaded loan documents (contracts, schedules, payment proofs) are
    # stored. The default is relative (local dev); in docker the compose file
    # pins it to the mounted /data volume so files survive container rebuilds.
    # Files are local-only (keep-pi-fully-local): nothing leaves
    # the LAN, no OCR. On-disk names are generated (UUID), never user input, so a
    # crafted filename can't escape this directory (no path traversal). See
    # attachments.py.
    attachments_path: Path = Path("data/attachments")
    # Largest single upload accepted, in bytes. A scanned multi-page contract PDF
    # is the realistic worst case; 10 MiB covers it comfortably. The allowed file
    # *types* are a security boundary kept in code (attachments.py), not env.
    attachment_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1)
    # Cap on how many documents one loan may hold — a contract, schedule and a
    # handful of payment proofs over the loan's life fit well under this. A sane
    # guard against an accidental mass upload filling the volume; raise it if a
    # loan legitimately needs more.
    attachment_max_per_loan: int = Field(default=50, ge=1)

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

    # --- myFund.pl investment import (Phase 6) -------------------------------
    # OPT-IN and OFF by default: with no API key the app makes *zero* outbound
    # calls and stays fully offline (design's core principle). Setting a key +
    # portfolio name turns on the only network egress in the app — a read-only
    # pull of your own portfolio. See internal_docs/PROGRESS.md (Phase 6).
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
        # A blank env value (e.g. EA_MYFUND_ACCOUNT_ID=) is the natural way to "leave
        # it unset" — treat it as None instead of crashing on int parsing. Scoped to
        # these optional ints: a blank *required* int stays an error, and "" is a
        # legitimate value for the str/SecretStr fields.
        if isinstance(value, str) and not value.strip():
            return None

        return value

    @property
    def myfund_configured(self) -> bool:
        """True once an API key and portfolio name are set — gates all egress."""
        return bool(self.myfund_api_key.get_secret_value() and self.myfund_portfolio)

    # --- Home Assistant via MQTT (Phase 7) -----------------------------------
    # OPT-IN and OFF by default: with no broker host the app makes *zero* MQTT
    # connections. Unlike myFund this is **not** internet egress — the broker is
    # your Home Assistant broker on the LAN (design §9). Setting a host turns on a
    # one-directional, read-only push of household metrics (net worth, monthly
    # spend, per-account balances) as auto-discovered HA sensors.
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

    # --- Subscription / recurring cost detection (Phase 9) -------------------
    # All derived from existing transactions — no egress, no opt-in. These tune
    # how forgiving the detector is; sensible defaults for a household, override
    # only if real data shows it's too eager or too shy. See subscriptions.py.
    #
    # Minimum charges from one merchant before it can be called recurring.
    subscription_min_occurrences: int = Field(default=3, ge=2)
    # How much a charge may differ from the merchant's typical amount (percent)
    # and still count as the "same" subscription — absorbs normal drift (FX, a
    # small plan tweak) without admitting variable spending at the same shop.
    subscription_amount_tolerance_pct: int = Field(default=15, ge=0)
    # How big a jump in the latest charge over the established price counts as
    # "it went up" before HA is pinged (design §13 open decision — tune with real
    # data). Independent of the tolerance above: that absorbs drift, this flags a
    # deliberate step.
    subscription_price_rise_pct: int = Field(default=10, ge=0)
    # A subscription whose first charge lands within this many days is "new" —
    # flagged in the UI and alerted to HA once (until you confirm it).
    subscription_new_window_days: int = Field(default=35, ge=1)

    # --- Categorization classifier (layer 2, Phase 11) -----------------------
    # Derived from existing labels — no egress, always on (like rules, unlike
    # myFund/MQTT). A TF-IDF + logistic-regression text classifier, trained fresh
    # on each run from human/rule-confirmed categorizations (no stored model). See
    # classifier.py / queries/classifier.py.
    #
    # A prediction at or above this confidence auto-applies the category
    # (source=classifier); below it the transaction stays uncategorized and shows
    # up in the review queue for a human to tag. Conservative by default — this is
    # a probabilistic guess landing on financial data, so it errs toward the queue.
    classifier_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    # Don't train (or classify) until at least this many confirmed labels exist —
    # below that the model is noise. Training also needs >= 2 distinct categories
    # (a logistic regression needs two classes). Until then it's a no-op and rows
    # simply wait in the queue.
    classifier_min_training_samples: int = Field(default=25, ge=2)

    # --- Categorization embeddings (layer 3, Phase 12) -----------------------
    # The "weird cases" fallback (design §7.7 point 3): embed each transaction and
    # find the most similar already-categorized one (kNN). Decorates the review
    # queue with that nearest example — a suggestion only, never auto-applied, so a
    # human still confirms the tag. No egress: the sentence-transformers model is
    # baked into the Docker image at build time and read offline at runtime (see the
    # Dockerfile). See embeddings.py / queries/embeddings.py.
    #
    # On by default, but fail-safe: if the model can't be loaded (e.g. a dev box
    # without the weights, or any offline process with nothing cached) the queue
    # simply shows no layer-3 hints. Set this False to skip the heavy path entirely.
    embeddings_enabled: bool = True
    # The sentence-transformers model. Multilingual (Polish bank descriptions) and
    # small enough for CPU inference on a Pi. Must be bundled in the image; changing
    # it means re-bundling. Overridable via EA_EMBEDDINGS_MODEL.
    embeddings_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    # Pin the model to an exact commit so the build is reproducible and the bundled
    # weights can't change under us. The runtime loader passes this revision too:
    # with HF_HUB_OFFLINE a commit-hash download has no "main" ref, so the offline
    # load must ask for the same revision it was bundled with. Change this in lockstep
    # with embeddings_model (a revision belongs to one repo).
    embeddings_model_revision: str = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
    # How many nearest labelled neighbours vote on the category for a queued row.
    embeddings_neighbors: int = Field(default=5, ge=1)
    # A neighbour below this cosine similarity (0..1) is too far to be a useful hint,
    # so no suggestion is shown — keeps the queue from guessing on novel merchants
    # that resemble nothing you've tagged.
    embeddings_min_similarity: float = Field(default=0.45, ge=0.0, le=1.0)
    # Don't build the index until at least this many confirmed labels exist (same
    # cold-start rationale as the classifier); also needs >= 2 distinct categories.
    embeddings_min_training_samples: int = Field(default=25, ge=2)

    # --- LLM categorization (piec Ollama; primary, local pipeline is fallback) ---
    # The owner runs Ollama on *piec* (a capable LAN box), so the heavy
    # categorization doesn't tax the Pi. When enabled, the LLM is the *primary*
    # categorizer for the review-queue "classify now" action and the local sklearn
    # classifier (layer 2) becomes the fallback for when piec is unreachable. Like
    # MQTT (and unlike myFund) this is LAN-only — piec is on the home network, so
    # it's not internet egress; nothing leaves the house. See ollama.py /
    # queries/categorize/llm.py.
    #
    # OPT-IN and OFF by default: with the feature off (or no base URL) the client is
    # never constructed and categorization behaves exactly as before.
    llm_enabled: bool = False
    # piec's Ollama base URL, e.g. "http://192.168.1.x:11434". Required when
    # llm_enabled is True (see the validator below).
    llm_base_url: str = ""
    # The model tag pulled on piec. A small "utility" model is the cheap fit for
    # classification; confirm the exact tag (overridable via EA_LLM_MODEL).
    llm_model: str = "gemma3:12b"
    # Read timeout in seconds for one chat call — LLM inference is slow, so this is
    # generous. (A short connect timeout, set in ollama.py, makes a *down* piec fail
    # fast rather than wait this long.)
    llm_timeout: int = Field(default=30, ge=1)
    # A verdict at or above this confidence auto-applies the category (source=llm);
    # below it the row stays in the review queue. Same conservative default as the
    # classifier — a probabilistic guess on financial data errs toward the queue.
    llm_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _require_base_url_when_llm_enabled(self) -> "Settings":
        # Enabling the LLM with no base URL is a silent no-op (the categorizer would
        # fall back to the classifier forever, looking like "the LLM isn't working").
        # Fail loud at startup instead.
        if self.llm_enabled and not self.llm_base_url.strip():
            raise ValueError(
                "EA_LLM_ENABLED is set but EA_LLM_BASE_URL is empty — "
                "set the piec Ollama URL, or disable the LLM."
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
        """Where the cron update check (``scripts/check_update.sh`` →
        ``ha.update_notify``) writes its verdict, for the in-app Updates view to
        read. Lives in the data volume; the web app only ever reads it — the
        network egress stays on the host's cron (keep-pi-fully-local)."""
        return self.data_path / "update_status.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
