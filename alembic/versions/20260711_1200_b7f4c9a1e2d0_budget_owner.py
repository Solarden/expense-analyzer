"""budget owner isolation

Adds ``Budget.owner_id`` so a **private** budget belongs to one member (NULL for a
shared household budget), and widens the uniqueness to
``(category_id, month, scope, owner_id)`` so two members can each hold their own
private limit for the same category/month. Backfills any pre-existing private
budget to the earliest admin (mirrors the transaction owner backfill), so it stays
visible to that admin once the per-owner predicate goes live. Round-trips on
downgrade for data with no per-owner private duplicates.

Revision ID: b7f4c9a1e2d0
Revises: ea04a52d8173
Create Date: 2026-07-11 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7f4c9a1e2d0"
down_revision: str | None = "ea04a52d8173"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("budget", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "owner_id",
                sa.Integer(),
                sa.ForeignKey("owner.id", name="fk_budget_owner_id_owner"),
                nullable=True,
            )
        )
        batch_op.create_index(batch_op.f("ix_budget_owner_id"), ["owner_id"], unique=False)
        batch_op.drop_constraint("uq_budget_category_month_scope", type_="unique")
        batch_op.create_unique_constraint(
            "uq_budget_category_month_scope_owner",
            ["category_id", "month", "scope", "owner_id"],
        )
    # Existing private budgets predate per-owner isolation -> assign to the earliest
    # admin so they stay visible to that admin. No admin (degenerate) -> no-op.
    op.execute(
        "UPDATE budget SET owner_id = "
        "(SELECT id FROM owner WHERE is_admin ORDER BY id LIMIT 1) "
        "WHERE scope = 'private' AND owner_id IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("budget", schema=None) as batch_op:
        batch_op.drop_constraint("uq_budget_category_month_scope_owner", type_="unique")
        batch_op.create_unique_constraint(
            "uq_budget_category_month_scope", ["category_id", "month", "scope"]
        )
        batch_op.drop_index(batch_op.f("ix_budget_owner_id"))
        batch_op.drop_column("owner_id")
