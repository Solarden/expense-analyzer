"""Smoke test: migrations apply cleanly to a fresh database.

Catches broken or un-runnable migrations the moment they land, before they
reach the Pi.
"""

import subprocess
import sys
from pathlib import Path

# tests/infra/test_migrations.py -> repo root is three levels up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_migrations_apply_to_fresh_db(tmp_path: Path):
    db = tmp_path / "migrate.db"
    env = {"EA_DATABASE_PATH": str(db)}

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**_os_environ(), **env},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"alembic failed:\n{result.stderr}"
    assert db.exists(), "migration run did not create the database file"


def _os_environ() -> dict[str, str]:
    import os

    return dict(os.environ)
