"""Tests for the SQLite backup helper (Phase 18).

The risky part is correctness of the copy: it must be a consistent, single-file
snapshot even with a live WAL writer, and pruning must keep the newest N.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from expense_analyzer.backup import BACKUP_PREFIX, create_backup, prune_backups


def _make_db(path: Path, rows: int) -> None:
    """Create a WAL-mode database with ``rows`` rows (some left uncheckpointed)."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row-{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


def _count(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def test_backup_is_a_consistent_copy(tmp_path: Path):
    src = tmp_path / "src.db"
    _make_db(src, rows=5)

    backup = create_backup(src, tmp_path / "backups")

    assert backup.exists()
    assert backup.parent == tmp_path / "backups"
    assert backup.name.startswith(BACKUP_PREFIX)
    assert _count(backup) == 5


def test_backup_captures_uncheckpointed_wal_writes(tmp_path: Path):
    # An open WAL connection with committed-but-unckeckpointed writes is the case
    # a naive `cp` of the .db file would miss. The online backup API must include them.
    src = tmp_path / "src.db"
    _make_db(src, rows=3)

    live = sqlite3.connect(src)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("INSERT INTO t (v) VALUES ('wal-only')")
    live.commit()  # in the -wal, not yet checkpointed into the main file
    try:
        backup = create_backup(src, tmp_path / "backups")
    finally:
        live.close()

    assert _count(backup) == 4


def test_missing_source_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        create_backup(tmp_path / "nope.db", tmp_path / "backups")


def test_same_second_backups_do_not_collide(tmp_path: Path):
    src = tmp_path / "src.db"
    _make_db(src, rows=1)
    fixed = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)

    first = create_backup(src, tmp_path / "backups", now=fixed)
    second = create_backup(src, tmp_path / "backups", now=fixed)

    assert first != second
    assert first.exists() and second.exists()


def test_prune_keeps_newest(tmp_path: Path):
    src = tmp_path / "src.db"
    _make_db(src, rows=1)
    dest = tmp_path / "backups"

    made = [
        create_backup(src, dest, keep=None, now=datetime(2026, 6, d, tzinfo=UTC))
        for d in range(1, 6)  # five backups, dated 1..5 June
    ]

    removed = prune_backups(dest, keep=2)

    assert sorted(removed) == sorted(made[:3])  # the three oldest are gone
    survivors = sorted(dest.glob(f"{BACKUP_PREFIX}*.db"))
    assert survivors == made[3:]  # the two newest remain


def test_prune_zero_keeps_all(tmp_path: Path):
    src = tmp_path / "src.db"
    _make_db(src, rows=1)
    dest = tmp_path / "backups"
    create_backup(src, dest, keep=None, now=datetime(2026, 6, 1, tzinfo=UTC))
    create_backup(src, dest, keep=None, now=datetime(2026, 6, 2, tzinfo=UTC))

    assert prune_backups(dest, keep=0) == []
    assert len(list(dest.glob(f"{BACKUP_PREFIX}*.db"))) == 2


def test_create_backup_prunes(tmp_path: Path):
    src = tmp_path / "src.db"
    _make_db(src, rows=1)
    dest = tmp_path / "backups"
    for d in range(1, 4):
        create_backup(src, dest, keep=2, now=datetime(2026, 6, d, tzinfo=UTC))

    assert len(list(dest.glob(f"{BACKUP_PREFIX}*.db"))) == 2


def _run_cli(*args: str):
    import subprocess
    import sys

    project_root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "-m", "expense_analyzer.backup", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
    )


def test_cli_if_exists_skips_missing_db_cleanly(tmp_path: Path):
    # The deploy's pre-migration backup uses --if-exists so a first deploy (no DB)
    # is a clean skip with empty stdout, not an error that aborts the deploy.
    missing = tmp_path / "nope.db"
    result = _run_cli("--database", str(missing), "--dest", str(tmp_path / "b"), "--if-exists")

    assert result.returncode == 0
    assert result.stdout.strip() == ""  # nothing for the deploy to capture


def test_cli_missing_db_without_flag_errors(tmp_path: Path):
    missing = tmp_path / "nope.db"
    result = _run_cli("--database", str(missing), "--dest", str(tmp_path / "b"))

    assert result.returncode != 0
    assert "not found" in result.stderr


def test_cli_prints_only_the_backup_path_on_stdout(tmp_path: Path):
    src = tmp_path / "src.db"
    _make_db(src, rows=2)
    result = _run_cli("--database", str(src), "--dest", str(tmp_path / "b"), "--if-exists")

    assert result.returncode == 0
    # stdout is exactly the backup path (deploy captures it); chatter goes to stderr.
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1
    assert Path(lines[0]).exists()
