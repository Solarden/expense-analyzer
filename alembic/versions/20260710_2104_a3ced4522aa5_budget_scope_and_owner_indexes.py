"""budget scope and owner indexes

Adds ``Budget.scope`` (separate private vs household category limits), reusing the
``scope`` enum created in phase 1 for ``transaction.scope``, and swaps the budget
uniqueness to ``(category_id, month, scope)``. Also indexes
``transaction.owner_id`` and ``account.owner_id`` — both become hot filter columns
once per-user scoping goes live. Dormant: no code reads these yet. Round-trips
cleanly on downgrade.

Revision ID: a3ced4522aa5
Revises: d3f8a1c02b47
Create Date: 2026-07-10 21:04:54.416032

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3ced4522aa5"
down_revision: str | None = "d3f8a1c02b47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Reuse the existing enum (created in phase 1 for transaction.scope);
    # create_type=False so PostgreSQL does not try to CREATE TYPE a second time.
    scope = sa.Enum("private", "household", name="scope", create_type=False)
    with op.batch_alter_table("budget", schema=None) as batch_op:
        batch_op.add_column(sa.Column("scope", scope, server_default="household", nullable=False))
        batch_op.drop_constraint("uq_budget_category_month", type_="unique")
        batch_op.create_unique_constraint(
            "uq_budget_category_month_scope", ["category_id", "month", "scope"]
        )
    with op.batch_alter_table("transaction", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_transaction_owner_id"), ["owner_id"], unique=False)
    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_account_owner_id"), ["owner_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("account", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_account_owner_id"))
    with op.batch_alter_table("transaction", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_transaction_owner_id"))
    with op.batch_alter_table("budget", schema=None) as batch_op:
        batch_op.drop_constraint("uq_budget_category_month_scope", type_="unique")
        batch_op.create_unique_constraint("uq_budget_category_month", ["category_id", "month"])
        batch_op.drop_column("scope")
