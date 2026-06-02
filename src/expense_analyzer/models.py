"""SQLModel table definitions.

Empty in Phase 0 by design. Phase 1 introduces Owner, Account, Transaction,
Category and ImportBatch here (see internal_docs/expense-analyzer-design.md §5).

Importing this module registers all tables on ``SQLModel.metadata``, which is
what Alembic autogenerate targets.
"""

from sqlmodel import SQLModel  # noqa: F401  (re-exported for Alembic's target_metadata)
