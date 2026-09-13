"""import batch owner

Adds ``import_batch.owner_id`` so a batch belongs to whoever imported it. A
rollback soft-deletes every row in the batch, including private ones the clicker
cannot see, so listing and rollback are gated on ownership.

Existing batches are backfilled from the owner of their transactions: a batch
whose rows all share one owner gets that owner. Batches with mixed or absent row
owners (and the shared Manual container) stay NULL, which no member can roll back.

Revision ID: c1d5e7a9b430
Revises: b7f4c9a1e2d0
Create Date: 2026-09-13 23:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d5e7a9b430"
down_revision: str | None = "b7f4c9a1e2d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("import_batch", schema=None) as batch_op:
        batch_op.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_import_batch_owner_id"), ["owner_id"], unique=False)
        batch_op.create_foreign_key("fk_import_batch_owner_id_owner", "owner", ["owner_id"], ["id"])

    # Correlated subquery rather than UPDATE..FROM, which SQLite and PostgreSQL
    # spell differently. MIN = MAX means every row shares one owner; when it does
    # not hold the HAVING yields no row and the column stays NULL.
    op.execute(
        """
        UPDATE import_batch
        SET owner_id = (
            SELECT MIN(t.owner_id)
            FROM "transaction" AS t
            WHERE t.import_batch_id = import_batch.id
              AND t.owner_id IS NOT NULL
            HAVING MIN(t.owner_id) = MAX(t.owner_id)
        )
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("import_batch", schema=None) as batch_op:
        batch_op.drop_constraint("fk_import_batch_owner_id_owner", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_import_batch_owner_id"))
        batch_op.drop_column("owner_id")
