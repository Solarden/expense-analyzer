"""backfill legacy owner to admin

Pre-per-user-scoping rows have ``owner_id = NULL`` (CSV imports never stamped an
owner). Assign them to the earliest admin — the household's setup account — so
they stay visible to that admin once the privacy predicate goes live; ``scope`` is
left ``private`` (the owner's decision: legacy history is not auto-shared). One-way
data backfill: which rows were originally NULL is not recorded, so there is
nothing to reverse on downgrade.

Revision ID: ea04a52d8173
Revises: a3ced4522aa5
Create Date: 2026-07-10 21:04:54.741040

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ea04a52d8173"
down_revision: str | None = "a3ced4522aa5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # "transaction" is a reserved word on both PostgreSQL and SQLite -> quote it.
    # No admin (degenerate: no users) -> the subquery is NULL and this is a no-op.
    op.execute(
        'UPDATE "transaction" SET owner_id = '
        "(SELECT id FROM owner WHERE is_admin ORDER BY id LIMIT 1) "
        "WHERE owner_id IS NULL"
    )


def downgrade() -> None:
    # One-way: the original NULLs are not recorded, so nothing is reversed.
    pass
