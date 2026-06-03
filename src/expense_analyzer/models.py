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
from enum import StrEnum

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


class TxSource(StrEnum):
    """Where a transaction's data (and its categorization) came from."""

    import_csv = "import_csv"
    manual = "manual"
    rule = "rule"
    classifier = "classifier"


class Owner(SQLModel, table=True):
    """A login identity. Originally the multi-user hook for ``owner_id``; now
    also the authenticatable user (username + password).

    No roles/permissions — every active user has the same shared household view
    (design's "scope is a tag, not a permission" still holds). ``owner_id`` on
    accounts/transactions stays an analytical tag (who imported), not a filter.
    """

    __tablename__ = "owner"

    id: int | None = Field(default=None, primary_key=True)
    name: str  # display name
    username: str = Field(unique=True, index=True)  # login handle
    password_hash: str
    is_active: bool = Field(default=True)  # deactivate without deleting
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


class Loan(SQLModel, table=True):
    """A loan/mortgage with a repayment schedule (design §5, §7.4).

    The amortization schedule is **not** stored — it's recomputed on the fly from
    these fields plus the variable-rate history (see :mod:`expense_analyzer.loans`),
    because a variable schedule changes retroactively when a base-rate observation
    is added. Money is integer minor units; the rate is integer **basis points**
    (7.25% == ``725``) so nothing is ever a float.
    """

    __tablename__ = "loan"

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id", index=True)  # the loan Account
    principal: int  # minor units, initial amount drawn
    rate_type: RateType
    # basis points. fixed: the annual rate; variable: the margin over the base rate.
    rate_bp: int
    base_rate_ref: str | None = Field(default=None)  # e.g. "WIBOR 3M" (variable)
    installment_type: InstallmentType
    start_date: date  # disbursement; first installment is one month later
    term_months: int
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
