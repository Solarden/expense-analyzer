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
    """Optional, future multi-user. No roles, no permissions (design §5).

    Exists only so ``owner_id`` columns mean something if we ever go
    multi-user. For now there is one implicit owner.
    """

    __tablename__ = "owner"

    id: int | None = Field(default=None, primary_key=True)
    name: str
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

    fingerprint: str = Field(unique=True, index=True)  # idempotency hash
    imported_at: datetime = Field(default_factory=utc_now)
    deleted_at: datetime | None = Field(default=None, index=True)  # soft delete
