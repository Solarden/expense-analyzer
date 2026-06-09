"""SQLModel table definitions — Phase 1 data model.

See internal_docs/expense-analyzer-design.md §5. Decisions baked in here:

- Money is stored as **integer minor units** (1/100 PLN), never float.
  ``amount`` is negative for expenses, positive for inflows.
- Every transaction carries a ``fingerprint`` (unique) for import idempotency
  and belongs to an :class:`ImportBatch` so a bad import rolls back in one move.
- Soft delete via ``deleted_at`` — nothing is ever truly destroyed.
- ``scope`` is an analytical tag (private vs household), **not** a permission.
- ``owner_id``, ``confidence`` and ``source`` exist from v1 so the later
  multi-user and auto-categorization work needs no migration.

Importing this module registers all tables on ``SQLModel.metadata``, which is
what Alembic autogenerate targets.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

from expense_analyzer.clock import utc_now


class AccountType(StrEnum):
    bank = "bank"
    portfolio = "portfolio"
    cash = "cash"
    loan = "loan"


class ImportStatus(StrEnum):
    active = "active"
    rolled_back = "rolled_back"


class CategoryKind(StrEnum):
    expense = "expense"
    income = "income"
    transfer = "transfer"


class RateType(StrEnum):
    """Interest rate type of a loan (design §5)."""

    fixed = "fixed"
    variable = "variable"  # ``Loan.rate_bp`` is the margin; base tracked separately


class InstallmentType(StrEnum):
    """Amortization style (design §7.4)."""

    equal = "equal"  # annuity: fixed total installment
    decreasing = "decreasing"  # fixed principal portion, shrinking interest


class Scope(StrEnum):
    """Analytical tag on a transaction. Not a permission (see design §1)."""

    private = "private"
    household = "household"


class SubscriptionStatus(StrEnum):
    """A user's verdict on a detected recurring-payment group (Phase 9)."""

    confirmed = "confirmed"  # yes, a real subscription
    dismissed = "dismissed"  # a false positive — hide it and stop alerting


class TxSource(StrEnum):
    """Where a transaction's data (and its categorization) came from."""

    import_csv = "import_csv"
    manual = "manual"
    rule = "rule"
    classifier = "classifier"


class Owner(SQLModel, table=True):
    """A login identity. Originally the multi-user hook for ``owner_id``; now
    also the authenticatable user (username + password).

    Data stays a single shared household view — every active user sees the same
    transactions (design's "scope is a tag, not a permission" still holds, and
    ``owner_id`` stays an analytical "who imported" tag, not a filter). The one
    distinction is ``is_admin``: a *soft* role gating user management (add/delete
    users, toggle active), not data isolation. The first user created (CLI
    bootstrap, while the table is empty) becomes admin; everyone after does not.
    """

    __tablename__ = "owner"

    id: int | None = Field(default=None, primary_key=True)
    name: str  # display name
    username: str = Field(unique=True, index=True)  # login handle
    password_hash: str
    is_active: bool = Field(default=True)  # deactivate without deleting
    is_admin: bool = Field(default=False)  # may manage other users (soft role)
    created_at: datetime = Field(default_factory=utc_now)


class Account(SQLModel, table=True):
    __tablename__ = "account"

    id: int | None = Field(default=None, primary_key=True)
    name: str  # "PKO checking", "IKE XTB", "Cash", "Mortgage"
    type: AccountType
    owner_id: int | None = Field(default=None, foreign_key="owner.id")
    currency: str = Field(default="PLN")
    created_at: datetime = Field(default_factory=utc_now)


class Category(SQLModel, table=True):
    __tablename__ = "category"

    id: int | None = Field(default=None, primary_key=True)
    name: str
    parent_id: int | None = Field(default=None, foreign_key="category.id")  # tree: Food > Groceries
    kind: CategoryKind
    # Optional display colour as a "#rrggbb" hex string (Phase 16). Drives the
    # swatch next to the category name and its series colour on the overview
    # chart. NULL = no colour chosen (legacy rows, or cleared) -> no swatch.
    color: str | None = Field(default=None)


class ImportBatch(SQLModel, table=True):
    """One CSV import. Transactions point here so a batch rolls back in one move."""

    __tablename__ = "import_batch"

    id: int | None = Field(default=None, primary_key=True)
    source: str  # "PKO csv", "mBank csv", "XTB csv"
    filename: str
    imported_at: datetime = Field(default_factory=utc_now)
    record_count: int = 0
    status: ImportStatus = Field(default=ImportStatus.active)


class Transaction(SQLModel, table=True):
    __tablename__ = "transaction"

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id", index=True)
    import_batch_id: int = Field(foreign_key="import_batch.id", index=True)

    # minor units (1/100 PLN): negative = expense, positive = inflow. Never a float.
    amount: int
    # Running balance from the CSV, used for reconciliation (design §6).
    balance_after: int | None = Field(default=None)
    booked_date: date = Field(index=True)

    raw_description: str
    merchant_normalized: str | None = Field(default=None)  # cleaned up, used by rules later
    note: str | None = Field(
        default=None
    )  # free-text human annotation (Phase 13); not categorization

    category_id: int | None = Field(default=None, foreign_key="category.id", index=True)
    scope: Scope = Field(default=Scope.private)
    owner_id: int | None = Field(default=None, foreign_key="owner.id")

    confidence: float | None = Field(default=None)  # categorization confidence (auto-tagging)
    source: TxSource = Field(default=TxSource.import_csv)
    transfer_group_id: str | None = Field(default=None, index=True)  # links the two sides

    # Plan-vs-reality (Phase 5): a real loan installment is an outflow (usually on
    # the checking account, not the loan account). ``loan_id`` marks it as a
    # payment toward a loan; ``loan_installment_index`` pins it to a specific
    # scheduled row (1-based) so a missed/double/prepaid month doesn't shift the
    # whole tail. Both nullable — a normal transaction has neither.
    loan_id: int | None = Field(default=None, foreign_key="loan.id", index=True)
    loan_installment_index: int | None = Field(default=None)

    fingerprint: str = Field(unique=True, index=True)  # idempotency hash
    imported_at: datetime = Field(default_factory=utc_now)
    deleted_at: datetime | None = Field(default=None, index=True)  # soft delete


class LoanBase(SQLModel):
    """Fields shared by the :class:`Loan` table and the :class:`LoanCreate` input.

    Money is integer minor units; the rate is integer **basis points**
    (7.25% == ``725``) so nothing is ever a float.
    """

    account_id: int = Field(foreign_key="account.id", index=True)  # the loan Account
    principal: int  # minor units, initial amount drawn
    rate_type: RateType
    # basis points. fixed: the annual rate; variable: the margin over the base rate.
    rate_bp: int
    base_rate_ref: str | None = Field(default=None)  # e.g. "WIBOR 3M" (variable)
    installment_type: InstallmentType
    start_date: date  # disbursement; first installment is one month later
    term_months: int


class Loan(LoanBase, table=True):
    """A loan/mortgage with a repayment schedule (design §5, §7.4).

    The amortization schedule is **not** stored — it's recomputed on the fly from
    these fields plus the variable-rate history (see :mod:`expense_analyzer.loans`),
    because a variable schedule changes retroactively when a base-rate observation
    is added.
    """

    __tablename__ = "loan"

    id: int | None = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=utc_now)


class LoanCreate(LoanBase):
    """Input for creating a loan (bundles what would otherwise be a long argument
    list to :func:`expense_analyzer.queries.loans.create_loan`).

    ``initial_base_rate_bp`` is **not** a Loan column: for a variable-rate loan it
    seeds the first :class:`LoanRateChange` (effective on ``start_date``) so the
    schedule has a rate from month 1. Ignored for a fixed-rate loan.
    """

    initial_base_rate_bp: int | None = None


class InvestmentPosition(SQLModel, table=True):
    """One holding in a portfolio account, as of a snapshot (design §5, §7.3).

    Informational: positions are imported once a month (XTB .xlsx) or pulled from
    the myFund.pl API — not a live feed. A snapshot is *latest-wins per date*: the
    natural key ``(account_id, ticker, snapshot_date)`` is unique, so re-importing
    the same day's export updates the row instead of duplicating it (no fingerprint
    / batch machinery — this is a state snapshot, not a stream of operations).

    Money stays integer minor units. ``quantity`` is the one deliberate
    :class:`~decimal.Decimal` in persistence — it's a fractional unit count, not
    money (a single ETF share can be 0.1980).
    """

    __tablename__ = "investment_position"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "ticker", "snapshot_date", name="uq_investment_position_snapshot"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id", index=True)
    ticker: str  # e.g. "SXR8.DE", "SNT.PL"
    quantity: Decimal  # fractional unit count (NOT money) — see class docstring

    value: int  # minor units: current market value of the holding (source-authoritative)
    avg_price: int | None = Field(default=None)  # minor units, average purchase price per unit
    current_price: int | None = Field(default=None)  # minor units, last price per unit
    currency: str = Field(default="PLN")

    snapshot_date: date = Field(index=True)
    source: str  # "xtb" | "myfund_api" | "manual"
    fetched_at: datetime = Field(default_factory=utc_now)


class Budget(SQLModel, table=True):
    """A per-category monthly spending limit (design §5, §7.6).

    ``month`` resolves the design's "a specific month *or* a recurring monthly
    limit" with one schema:

    - ``month is None`` — the **recurring** default limit for the category, in
      force every month unless overridden.
    - ``month == "YYYY-MM"`` — a one-off **override** for that single month
      (e.g. a bigger food budget in December), which wins over the recurring
      default for that month only.

    ``limit_amount`` is integer minor units (positive). Budgets are an analytical
    overlay on spending — they touch no transaction and need no transfer/loan
    machinery.

    The ``(category_id, month)`` unique constraint guards against duplicate
    overrides for the same month. SQLite treats ``NULL`` months as *distinct*, so
    it does **not** stop a second recurring row on its own — the single-writer
    query layer enforces "one recurring per category" by upserting (find-or-update
    by category + month) in :func:`expense_analyzer.queries.budgets.set_budget`.
    """

    __tablename__ = "budget"
    __table_args__ = (UniqueConstraint("category_id", "month", name="uq_budget_category_month"),)

    id: int | None = Field(default=None, primary_key=True)
    category_id: int = Field(foreign_key="category.id", index=True)
    month: str | None = Field(default=None)  # None = recurring default; "YYYY-MM" = override
    limit_amount: int  # minor units, positive


class Subscription(SQLModel, table=True):
    """A user's verdict on a detected recurring-payment group (design §7.5, §11).

    Subscriptions themselves are **derived**, not stored: the recurring-cost view
    is recomputed live from transaction history (a merchant grouping plus
    regularity of date and amount — see :mod:`expense_analyzer.subscriptions`).
    This table persists only the *human verdict* over a detected group, keyed by
    its grouping key ``merchant`` (the transaction's ``merchant_normalized``):

    - ``confirmed`` — acknowledged as a real subscription. Stops the "new
      subscription detected" alert (you already know about it).
    - ``dismissed`` — a false positive. Hidden from the suggestions list, excluded
      from the "fixed monthly costs" total, and never alerts. Sticky: it stays
      dismissed even if the merchant recurs later.

    A detected group with no row here is simply a live *suggestion*. Storing the
    verdict (rather than nothing) is the one deliberate step beyond "purely
    derived" — it lets the user prune false positives and silence known
    subscriptions without that state evaporating on the next page load.
    """

    __tablename__ = "subscription"

    id: int | None = Field(default=None, primary_key=True)
    # The grouping key: a transaction's normalized merchant. Unique — one verdict
    # per merchant (the single-writer query layer upserts on it).
    merchant: str = Field(unique=True, index=True)
    status: SubscriptionStatus
    created_at: datetime = Field(default_factory=utc_now)


class Rule(SQLModel, table=True):
    """A categorization rule — layer 1 of categorization (design §5, §7.7).

    The first, deterministic categorization layer: a case-insensitive substring
    ``pattern`` matched against a transaction's ``merchant_normalized`` (falling
    back to ``raw_description`` when no merchant was extracted) assigns
    ``category_id``. Spending is repetitive, so a handful of substring rules covers
    most of it without any ML. ``priority`` orders the rules — the highest-priority
    matching rule wins, ties broken by ``id`` (the older rule first).

    Rules run automatically on import and on demand ("Apply rules now"). The pure
    matcher lives in :mod:`expense_analyzer.rules`; the DB side (CRUD + apply) in
    :mod:`expense_analyzer.queries.rules`. A rule only ever (re)categorizes a row
    that is uncategorized or was itself set by a rule (``source = rule``) — a
    human's manual categorization is never overwritten.
    """

    __tablename__ = "rule"

    id: int | None = Field(default=None, primary_key=True)
    pattern: str  # case-insensitive substring matched against merchant / description
    category_id: int = Field(foreign_key="category.id", index=True)
    priority: int = Field(default=0)  # higher wins; ties broken by id (older first)
    created_at: datetime = Field(default_factory=utc_now)


class LoanRateChange(SQLModel, table=True):
    """A base-rate observation for a variable-rate loan (e.g. a WIBOR fix).

    The effective annual rate from ``effective_date`` onward is
    ``base_rate_bp + Loan.rate_bp`` (margin). A variable loan needs at least one
    row with ``effective_date <= Loan.start_date`` so month 1 has a rate.
    """

    __tablename__ = "loan_rate_change"

    id: int | None = Field(default=None, primary_key=True)
    loan_id: int = Field(foreign_key="loan.id", index=True)
    effective_date: date = Field(index=True)
    base_rate_bp: int  # basis points
