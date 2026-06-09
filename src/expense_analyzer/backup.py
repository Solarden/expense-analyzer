"""Back up the SQLite database to a timestamped copy.

Uses SQLite's online backup API, so it is safe to run while the app is live
(WAL writers included) and always produces a single consistent file — there is
no need to copy the ``-wal``/``-shm`` sidecars by hand. Doubles as the design
§10 cron backup and as the pre-migration safety copy taken by
``scripts/deploy.sh`` (Phase 18).

    python -m expense_analyzer.backup                 # back up the configured DB
    python -m expense_analyzer.backup --keep 30       # keep the 30 newest backups
    python -m expense_analyzer.backup --keep 0         # keep all (no pruning)

The backup path is printed on stdout (everything else goes to stderr) so callers
can capture just the path: ``dest=$(python -m expense_analyzer.backup)``.
"""

import argparse
import contextlib
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from expense_analyzer.config import get_settings

BACKUP_DIR_NAME = "backups"
BACKUP_PREFIX = "expense_analyzer-"
BACKUP_SUFFIX = ".db"
# UTC timestamp (the app works in UTC internally — no DST ambiguity in filenames).
# Sorts lexicographically == chronologically, which prune relies on.
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%SZ"
DEFAULT_KEEP = 14


def create_backup(
    src: Path,
    dest_dir: Path,
    *,
    keep: int | None = DEFAULT_KEEP,
    now: datetime | None = None,
) -> Path:
    """Copy ``src`` to a timestamped file in ``dest_dir`` and return its path.

    The copy is taken with SQLite's online backup API, so a concurrent writer
    cannot corrupt it. Pass ``keep`` to prune older backups down to that many of
    the newest (``None`` keeps everything).
    """
    if not src.exists():
        raise FileNotFoundError(f"database not found: {src}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime(TIMESTAMP_FORMAT)
    dest = dest_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    # Guard against two backups landing in the same second (deploy + manual cron).
    counter = 1
    while dest.exists():
        dest = dest_dir / f"{BACKUP_PREFIX}{stamp}-{counter}{BACKUP_SUFFIX}"
        counter += 1

    # The backup API never writes to the source; a plain (read-write) connection
    # is the standard, most-portable way to open it (a read-only WAL source can
    # fail when the -shm is absent).
    with contextlib.closing(sqlite3.connect(src)) as source:
        with contextlib.closing(sqlite3.connect(dest)) as target:
            source.backup(target)

    if keep is not None:
        prune_backups(dest_dir, keep)

    return dest


def prune_backups(dest_dir: Path, keep: int) -> list[Path]:
    """Delete all but the ``keep`` newest backups in ``dest_dir``; return removed.

    ``keep <= 0`` is a no-op (keep everything). Newness is decided by filename,
    which embeds a sortable UTC timestamp.
    """
    if keep <= 0:
        return []

    backups = sorted(p for p in dest_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}") if p.is_file())
    removed = backups[:-keep] if len(backups) > keep else []
    for old in removed:
        old.unlink()
        print(f"pruned old backup: {old}", file=sys.stderr)

    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up the Expense Analyzer SQLite database.")
    parser.add_argument(
        "--database",
        type=Path,
        help="database file to back up (defaults to EA_DATABASE_PATH)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        help="directory to write backups into (defaults to <database parent>/backups)",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=DEFAULT_KEEP,
        help=f"keep this many newest backups, 0 keeps all (default {DEFAULT_KEEP})",
    )
    parser.add_argument(
        "--if-exists",
        action="store_true",
        help="skip silently (exit 0) if the database doesn't exist yet, instead of erroring "
        "— used by the deploy's pre-migration backup, where a first deploy has no DB",
    )
    args = parser.parse_args()

    src = args.database or get_settings().database_path
    dest_dir = args.dest or (src.parent / BACKUP_DIR_NAME)
    keep = None if args.keep <= 0 else args.keep

    if args.if_exists and not src.exists():
        print(f"no database at {src} — nothing to back up", file=sys.stderr)
        return

    try:
        backup = create_backup(src, dest_dir, keep=keep)
    except FileNotFoundError as exc:
        sys.exit(f"error: {exc}")

    print(f"backed up {src} -> {backup}", file=sys.stderr)
    print(backup)


if __name__ == "__main__":
    main()
