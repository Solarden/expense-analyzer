"""Smoke test: migrations apply cleanly to a fresh database.

Catches broken or un-runnable migrations the moment they land, before they
reach the Pi. Runs against whatever engine the suite runs on — on the default
PostgreSQL test server this is the test that proves the whole migration
history works on the production dialect (e.g. the phase 15 ``is_admin = TRUE``
fix); on a sqlite EA_TEST_DATABASE_URL override it falls back to a temp file.
"""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

# tests/infra/test_migrations.py -> repo root is three levels up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

SCRATCH_DB = "ea_migrate_smoke"


def test_migrations_apply_to_fresh_db(tmp_path: Path):
    url = make_url(os.environ["EA_DATABASE_URL"])

    if url.get_backend_name() == "sqlite":
        db = tmp_path / "migrate.db"
        result = _run_alembic(f"sqlite:///{db}")

        assert result.returncode == 0, f"alembic failed:\n{result.stderr}"
        assert db.exists(), "migration run did not create the database file"
        return

    # Server database: migrations must run on a FRESH database, not the suite's
    # (its schema came from create_all). Create a scratch one next to it.
    admin = create_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    scratch_url = url.set(database=SCRATCH_DB).render_as_string(hide_password=False)
    try:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
            conn.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))

        result = _run_alembic(scratch_url)
        assert result.returncode == 0, f"alembic failed:\n{result.stderr}"

        smoke = create_engine(scratch_url, poolclass=NullPool)
        with smoke.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        smoke.dispose()
        assert version, "alembic_version is empty — migrations did not stamp head"
    finally:
        with admin.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        admin.dispose()


def _run_alembic(database_url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "EA_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
