"""Back up (and restore) the configured database.

Dialect-aware, driven by ``EA_DATABASE_URL``:

* **PostgreSQL** (production — the shared /opt/stack server): ``pg_dump
  --format=custom`` to a timestamped ``.dump``; restore validates the archive,
  resets the ``public`` schema, then runs ``pg_restore`` into the same
  database. The client binaries come from the Docker image (see the
  Dockerfile's pinned ``postgresql-client`` — keep its major in lockstep with
  the server's).
* **SQLite** (local dev): SQLite's online backup API, safe to run while the
  app is live (WAL writers included) — always a single consistent ``.db`` file.

Doubles as the design §10 cron backup and as the pre-migration safety copy
taken by ``scripts/deploy.sh`` (Phase 18), which also calls ``--restore`` on
rollback.

    python -m expense_analyzer.backup                 # back up the configured DB
    python -m expense_analyzer.backup --keep 30       # keep the 30 newest backups
    python -m expense_analyzer.backup --keep 0        # keep all (no pruning)
    python -m expense_analyzer.backup --restore data/backups/<file>

The backup path is printed on stdout (everything else goes to stderr) so callers
can capture just the path: ``dest=$(python -m expense_analyzer.backup)``.
"""

import argparse
import contextlib
import os
import shutil
import sqlite3
import subprocess  # nosec B404 — pg_dump/pg_restore wrappers; argv lists, never a shell
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from expense_analyzer.config import get_settings

BACKUP_DIR_NAME = "backups"
BACKUP_PREFIX = "expense_analyzer-"
BACKUP_SUFFIX = ".db"
PG_BACKUP_SUFFIX = ".dump"
# UTC timestamp (the app works in UTC internally — no DST ambiguity in filenames).
# Sorts lexicographically == chronologically, which prune relies on.
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%SZ"
DEFAULT_KEEP = 14


class BackupError(RuntimeError):
    """A backup/restore tool invocation failed (message = its stderr)."""


def create_backup(
    src: Path,
    dest_dir: Path,
    *,
    keep: int | None = DEFAULT_KEEP,
    now: datetime | None = None,
) -> Path:
    """Copy SQLite ``src`` to a timestamped file in ``dest_dir``; return its path.

    The copy is taken with SQLite's online backup API, so a concurrent writer
    cannot corrupt it. Pass ``keep`` to prune older backups down to that many of
    the newest (``None`` keeps everything).
    """
    if not src.exists():
        raise FileNotFoundError(f"database not found: {src}")

    dest = _next_backup_path(dest_dir, BACKUP_SUFFIX, now)

    # The backup API never writes to the source; a plain (read-write) connection
    # is the standard, most-portable way to open it (a read-only WAL source can
    # fail when the -shm is absent).
    with contextlib.closing(sqlite3.connect(src)) as source:
        with contextlib.closing(sqlite3.connect(dest)) as target:
            source.backup(target)

    if keep is not None:
        prune_backups(dest_dir, keep, suffix=BACKUP_SUFFIX)

    return dest


def create_pg_backup(
    url: URL,
    dest_dir: Path,
    *,
    keep: int | None = DEFAULT_KEEP,
    now: datetime | None = None,
) -> Path:
    """``pg_dump`` the database behind ``url`` into ``dest_dir``; return the path.

    Custom format (``-Fc``): compressed and ``pg_restore``-able. A failed dump
    removes its partial file and raises :class:`BackupError` with pg_dump's
    stderr, so callers can tell "database does not exist" from a real failure.
    """
    dest = _next_backup_path(dest_dir, PG_BACKUP_SUFFIX, now)
    conn_args, env = _pg_conn_args(url)
    # nosec B603 B607 — fixed argv from our own config URL (no shell); the bare
    # binary name is on purpose: PATH resolves it in the image and on dev boxes.
    result = subprocess.run(  # nosec B603 B607
        ["pg_dump", *conn_args, "--format", "custom", "--file", str(dest), url.database or ""],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        dest.unlink(missing_ok=True)
        raise BackupError(result.stderr.strip() or "pg_dump failed")

    if keep is not None:
        prune_backups(dest_dir, keep, suffix=PG_BACKUP_SUFFIX)

    return dest


def restore_backup(url: URL, backup_file: Path) -> None:
    """Restore the configured database from ``backup_file`` (dialect-aware).

    PostgreSQL: the schema is reset wholesale before ``pg_restore``. A plain
    ``pg_restore --clean`` would drop only objects *present in the dump* — but
    a rolled-back deploy can leave objects the dump doesn't know about (a
    migration that COMMITTED before the app failed health), and those would
    survive and wedge every following deploy on "already exists". SQLite: copy
    the file back and drop stale WAL/SHM sidecars so it's read as-is.
    """
    if not backup_file.exists():
        raise FileNotFoundError(f"backup not found: {backup_file}")

    if url.get_backend_name() == "sqlite":
        db_path = Path(url.database or "")
        shutil.copyfile(backup_file, db_path)
        Path(f"{db_path}-wal").unlink(missing_ok=True)
        Path(f"{db_path}-shm").unlink(missing_ok=True)

        return

    # Everything that can fail without the server's help is checked BEFORE the
    # schema drop — dropping first and then discovering a bad argument (e.g. a
    # legacy .db file tab-completed from the same backups dir) or a missing
    # binary would leave the database empty with nothing restored.
    if shutil.which("pg_restore") is None:
        raise BackupError("pg_restore not found — install postgresql-client (see Dockerfile)")
    _validate_pg_archive(backup_file)

    _reset_pg_schema(url)
    conn_args, env = _pg_conn_args(url)
    result = subprocess.run(  # nosec B603 B607 — same rationale as the pg_dump call
        [
            "pg_restore",
            *conn_args,
            "--no-owner",
            "--dbname",
            url.database or "",
            str(backup_file),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BackupError(result.stderr.strip() or "pg_restore failed")


def _validate_pg_archive(backup_file: Path) -> None:
    """Refuse anything that isn't a pg_restore archive — before any drop."""
    result = subprocess.run(  # nosec B603 B607 — same rationale as the pg_dump call
        ["pg_restore", "--list", str(backup_file)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BackupError(f"not a pg_restore archive: {backup_file} ({result.stderr.strip()})")


def _reset_pg_schema(url: URL) -> None:
    """Drop and recreate the ``public`` schema — a clean slate for pg_restore.

    Works without superuser: since PG 15 ``public`` is owned by the database
    owner, which is the app's role (see the README's one-time bootstrap).
    IF EXISTS on the drop: if an earlier restore attempt died between DROP and
    CREATE, a re-run must heal the state, not trip over the missing schema.
    """
    engine = create_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with engine.connect() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    except SQLAlchemyError as exc:
        raise BackupError(f"schema reset failed: {exc}") from exc
    finally:
        engine.dispose()


def prune_backups(dest_dir: Path, keep: int, *, suffix: str = BACKUP_SUFFIX) -> list[Path]:
    """Delete all but the ``keep`` newest backups in ``dest_dir``; return removed.

    ``keep <= 0`` is a no-op (keep everything). Newness is decided by filename,
    which embeds a sortable UTC timestamp.
    """
    if keep <= 0:
        return []

    backups = sorted(p for p in dest_dir.glob(f"{BACKUP_PREFIX}*{suffix}") if p.is_file())
    removed = backups[:-keep] if len(backups) > keep else []
    for old in removed:
        old.unlink()
        print(f"pruned old backup: {old}", file=sys.stderr)

    return removed


def _next_backup_path(dest_dir: Path, suffix: str, now: datetime | None) -> Path:
    """A fresh timestamped path in ``dest_dir`` (creating it if needed)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime(TIMESTAMP_FORMAT)
    dest = dest_dir / f"{BACKUP_PREFIX}{stamp}{suffix}"
    # Guard against two backups landing in the same second (deploy + manual cron).
    counter = 1
    while dest.exists():
        dest = dest_dir / f"{BACKUP_PREFIX}{stamp}-{counter}{suffix}"
        counter += 1

    return dest


def _pg_conn_args(url: URL) -> tuple[list[str], dict[str, str]]:
    """pg_dump/pg_restore connection argv + env, from the SQLAlchemy URL.

    The password travels via PGPASSWORD in the environment, never argv (argv is
    world-readable in /proc). --no-password: fail fast instead of prompting —
    this runs non-interactively (cron, deploy).
    """
    args = [
        "--host",
        url.host or "localhost",
        "--port",
        str(url.port or 5432),
        "--username",
        url.username or "",
        "--no-password",
    ]
    env = {**os.environ, "PGPASSWORD": url.password or ""}

    return args, env


def _configured_sqlite_path() -> Path:
    """The SQLite file behind EA_DATABASE_URL (callers ensured it's sqlite)."""
    url = make_url(get_settings().database_url)

    return Path(url.database or "")


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up the Expense Analyzer database.")
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite file to back up (sqlite only; defaults to the EA_DATABASE_URL path)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        help="directory to write backups into (defaults to <data dir>/backups)",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=None,
        help="keep this many newest backups, 0 keeps all "
        f"(default: EA_BACKUP_KEEP, falling back to {DEFAULT_KEEP})",
    )
    parser.add_argument(
        "--if-exists",
        action="store_true",
        help="skip silently (exit 0) if there is no database yet, instead of erroring "
        "— used by the deploy's pre-migration backup, where a first deploy has no DB",
    )
    parser.add_argument(
        "--restore",
        type=Path,
        metavar="FILE",
        help="restore the configured database from FILE instead of backing up "
        "— used by the deploy's rollback",
    )
    args = parser.parse_args()

    url = make_url(get_settings().database_url)
    is_sqlite = url.get_backend_name() == "sqlite"

    if args.restore is not None:
        try:
            restore_backup(url, args.restore)
        except (FileNotFoundError, BackupError) as exc:
            sys.exit(f"error: {exc}")
        print(f"restored database from {args.restore}", file=sys.stderr)

        return

    if not is_sqlite and args.database:
        sys.exit("error: --database applies to sqlite only; EA_DATABASE_URL is a server DB")

    keep_value = args.keep if args.keep is not None else get_settings().backup_keep
    keep = None if keep_value <= 0 else keep_value

    if is_sqlite:
        src = args.database or _configured_sqlite_path()
        dest_dir = args.dest or (src.parent / BACKUP_DIR_NAME)
        if args.if_exists and not src.exists():
            print(f"no database at {src} — nothing to back up", file=sys.stderr)
            return
        try:
            backup = create_backup(src, dest_dir, keep=keep)
        except FileNotFoundError as exc:
            sys.exit(f"error: {exc}")
        print(f"backed up {src} -> {backup}", file=sys.stderr)
    else:
        dest_dir = args.dest or (get_settings().data_path / BACKUP_DIR_NAME)
        try:
            backup = create_pg_backup(url, dest_dir, keep=keep)
        except BackupError as exc:
            # Anchored to the MISSING-DATABASE error specifically: a bare
            # "does not exist" would also match e.g. a mistyped role
            # (FATAL: role "..." does not exist) and silently skip the one
            # safety backup of a database that very much exists.
            # The FATAL text comes from the SERVER, so a non-English
            # lc_messages would break the match — failing SAFE (deploy aborts
            # instead of proceeding backup-less).
            if args.if_exists and f'database "{url.database}" does not exist' in str(exc):
                print(f"no database on the server yet — nothing to back up: {exc}", file=sys.stderr)
                return
            sys.exit(f"error: {exc}")
        except FileNotFoundError:
            sys.exit("error: pg_dump not found — install postgresql-client (see Dockerfile)")
        print(f"backed up {url.host}/{url.database} -> {backup}", file=sys.stderr)

    print(backup)


if __name__ == "__main__":
    main()
