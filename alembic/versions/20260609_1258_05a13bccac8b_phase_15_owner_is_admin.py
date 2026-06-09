"""phase 15 owner is_admin

Revision ID: 05a13bccac8b
Revises: 0a310efa5193
Create Date: 2026-06-09 12:58:57.483148

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "05a13bccac8b"
down_revision: str | None = "0a310efa5193"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default=false backfills existing rows so the NOT NULL column can be
    # added to a populated table (and new rows match the model's app-side default).
    with op.batch_alter_table("owner", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false())
        )

    # Promote the earliest existing user to admin so an already-running household
    # keeps a working admin (no lockout). On a fresh DB there are no rows, so this
    # is a no-op and the first user created later bootstraps as admin instead.
    op.execute("UPDATE owner SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM owner)")


def downgrade() -> None:
    with op.batch_alter_table("owner", schema=None) as batch_op:
        batch_op.drop_column("is_admin")
