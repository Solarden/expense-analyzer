"""share existing transactions with the household

``scope`` defaults to ``private``, which was chosen while it was an analytical tag
and kept when it became the visibility boundary. The result is a home budget that
is empty by construction: nothing an importer produced ever reached it. The scope
of an imported row now comes from its account — a shared account imports
``household``, a member's own account imports ``private`` — and this hands the
existing history to the household so those figures are true from the first run.

No account has an owner at this point (nothing could set one before this release),
so "every row" *is* the per-account rule applied to today's data; the two agree.
That equivalence lasts only while that holds: give an account an owner first and
its history is shared here anyway. Migrations run before the app starts
(``scripts/deploy.sh``), so the deploy is safe; locally, migrate before using the
new owner picker.

One-way: which rows were private is not recorded, so downgrade cannot restore it.
The control is the backup ``scripts/deploy.sh`` takes before migrating — recovery
is a restore, not a downgrade.

Revision ID: d4a17c0b3e52
Revises: c1d5e7a9b430
Create Date: 2026-09-15 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4a17c0b3e52"
down_revision: str | None = "c1d5e7a9b430"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # "transaction" is a reserved word on both PostgreSQL and SQLite -> quote it.
    op.execute("UPDATE \"transaction\" SET scope = 'household' WHERE scope = 'private'")


def downgrade() -> None:
    # One-way: which rows were private is not recorded, so nothing is reversed.
    pass
