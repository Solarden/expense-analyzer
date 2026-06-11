"""Tests for the backup helper (Phase 18; PostgreSQL branch in Phase 23).

SQLite: the risky part is correctness of the copy — a consistent, single-file
snapshot even with a live WAL writer, and pruning keeping the newest N.
PostgreSQL: the tool wraps pg_dump/pg_restore, so the tests pin the exact
invocation (argv + PGPASSWORD env, never argv) and the failure cleanup.
"""

import contextlib
import sqlite3
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from expense_analyzer import backup as backup_mod
from expense_analyzer.backup import (
    BACKUP_PREFIX,
    BackupError,
    create_backup,
    create_pg_backup,
    prune_backups,
    restore_backup,
)


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


# --- PostgreSQL branch ------------------------------------------------------

PG_URL = make_url("postgresql+psycopg://ea:s3cret@db.lan:5433/expenses")


class FakeRun:
    """Records a subprocess.run call; optionally fails with canned stderr."""

    def __init__(self, *, returncode: int = 0, stderr: str = ""):
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "pg_dump":
            # Real pg_dump creates the --file target even on failure — mimic
            # that, so the partial-file-cleanup assertion actually bites.
            Path(argv[argv.index("--file") + 1]).touch()

        return types.SimpleNamespace(returncode=self.returncode, stderr=self.stderr)


def test_pg_backup_invokes_pg_dump_with_password_in_env(tmp_path: Path, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(backup_mod.subprocess, "run", fake)

    dest = create_pg_backup(PG_URL, tmp_path / "backups", now=datetime(2026, 6, 11, tzinfo=UTC))

    argv, kwargs = fake.calls[0]
    # The exact invocation IS the contract — pin it whole.
    assert argv == [
        "pg_dump",
        "--host",
        "db.lan",
        "--port",
        "5433",
        "--username",
        "ea",
        "--no-password",
        "--format",
        "custom",
        "--file",
        str(dest),
        "expenses",
    ]
    assert kwargs["env"]["PGPASSWORD"] == "s3cret"
    assert "s3cret" not in " ".join(argv)  # the password never reaches argv
    assert dest.exists()
    assert dest.suffix == ".dump"
    assert dest.name.startswith(BACKUP_PREFIX)


def test_pg_backup_failure_raises_and_removes_partial_file(tmp_path: Path, monkeypatch):
    fake = FakeRun(returncode=1, stderr='pg_dump: error: database "expenses" does not exist')
    monkeypatch.setattr(backup_mod.subprocess, "run", fake)

    with pytest.raises(BackupError, match="does not exist"):
        create_pg_backup(PG_URL, tmp_path / "backups")

    assert list((tmp_path / "backups").glob("*")) == []  # no partial dump left behind


def test_pg_backup_prunes_dumps(tmp_path: Path, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(backup_mod.subprocess, "run", fake)
    dest = tmp_path / "backups"

    for d in range(1, 4):
        create_pg_backup(PG_URL, dest, keep=2, now=datetime(2026, 6, d, tzinfo=UTC))

    assert len(list(dest.glob(f"{BACKUP_PREFIX}*.dump"))) == 2


def test_pg_restore_resets_schema_then_runs_pg_restore(tmp_path: Path, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(backup_mod.subprocess, "run", fake)
    resets: list = []
    monkeypatch.setattr(backup_mod, "_reset_pg_schema", resets.append)
    dump = tmp_path / "x.dump"
    dump.touch()

    restore_backup(PG_URL, dump)

    # Schema reset must come first — it replaces --clean (which would leave
    # behind objects a committed-then-rolled-back migration created).
    assert resets == [PG_URL]
    argv, kwargs = fake.calls[0]
    assert argv == [
        "pg_restore",
        "--host",
        "db.lan",
        "--port",
        "5433",
        "--username",
        "ea",
        "--no-password",
        "--no-owner",
        "--dbname",
        "expenses",
        str(dump),
    ]
    assert kwargs["env"]["PGPASSWORD"] == "s3cret"


def test_restore_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        restore_backup(PG_URL, tmp_path / "nope.dump")


def test_sqlite_restore_copies_file_and_drops_sidecars(tmp_path: Path):
    db = tmp_path / "app.db"
    _make_db(db, rows=1)
    backup = create_backup(db, tmp_path / "backups")
    # Diverge the live DB and leave stale WAL sidecars behind.
    with contextlib.closing(sqlite3.connect(db)) as live:
        live.execute("DROP TABLE t")
        live.commit()
    (tmp_path / "app.db-wal").write_bytes(b"stale")
    (tmp_path / "app.db-shm").write_bytes(b"stale")

    restore_backup(make_url(f"sqlite:///{db}"), backup)

    # Sidecars first: the _count below reopens the (WAL-mode) file and would
    # recreate fresh ones, masking whether restore dropped the stale pair.
    assert not (tmp_path / "app.db-wal").exists()
    assert not (tmp_path / "app.db-shm").exists()
    assert _count(db) == 1  # the pre-divergence content is back


def _run_cli(*args: str, database_url: str = "sqlite:///data/unused.db"):
    import os
    import subprocess
    import sys

    project_root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "-m", "expense_analyzer.backup", *args],
        cwd=project_root,
        # The CLI reads EA_DATABASE_URL; the suite's own (postgres) URL must
        # not leak into the child, so each test pins the URL it exercises.
        env={**os.environ, "EA_DATABASE_URL": database_url},
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


def test_cli_restore_round_trips_sqlite(tmp_path: Path):
    # The exact CLI the deploy's rollback runs: python -m ... --restore <file>.
    db = tmp_path / "app.db"
    _make_db(db, rows=3)
    backup = create_backup(db, tmp_path / "backups")
    with contextlib.closing(sqlite3.connect(db)) as live:
        live.execute("DROP TABLE t")
        live.commit()

    result = _run_cli("--restore", str(backup), database_url=f"sqlite:///{db}")

    assert result.returncode == 0
    assert result.stdout.strip() == ""  # restore prints nothing on stdout
    assert "restored" in result.stderr
    assert _count(db) == 3


def test_cli_restore_missing_file_errors(tmp_path: Path):
    result = _run_cli("--restore", str(tmp_path / "nope.dump"))

    assert result.returncode != 0
    assert "not found" in result.stderr


def test_cli_database_flag_rejected_on_server_url(tmp_path: Path):
    result = _run_cli(
        "--database",
        str(tmp_path / "x.db"),
        database_url="postgresql+psycopg://ea:pw@db.lan:5432/expenses",
    )

    assert result.returncode != 0
    assert "sqlite only" in result.stderr


def test_prune_is_suffix_scoped(tmp_path: Path):
    # PG dumps and sqlite dev backups share data/backups — pruning one format
    # must never count or delete the other.
    dest = tmp_path / "backups"
    dest.mkdir()
    db_files = [dest / f"{BACKUP_PREFIX}2026060{d}_000000Z.db" for d in range(1, 4)]
    dump_files = [dest / f"{BACKUP_PREFIX}2026060{d}_000000Z.dump" for d in range(1, 4)]
    for p in db_files + dump_files:
        p.touch()

    prune_backups(dest, keep=1, suffix=".dump")

    assert sorted(dest.glob("*.db")) == db_files  # untouched
    assert sorted(dest.glob("*.dump")) == dump_files[-1:]


def _fake_settings(url: str, data_path: Path, backup_keep: int = 14):
    return types.SimpleNamespace(database_url=url, data_path=data_path, backup_keep=backup_keep)


def test_keep_defaults_to_settings_backup_keep(tmp_path: Path, monkeypatch, capsys):
    # `make backup` / cron pass no --keep; retention must follow EA_BACKUP_KEEP.
    db = tmp_path / "app.db"
    _make_db(db, rows=1)
    monkeypatch.setattr(
        backup_mod,
        "get_settings",
        lambda: _fake_settings(f"sqlite:///{db}", tmp_path, backup_keep=2),
    )
    seen_keep: list = []

    def record(src, dest_dir, *, keep):
        seen_keep.append(keep)

        return tmp_path / "fake-backup.db"

    monkeypatch.setattr(backup_mod, "create_backup", record)
    monkeypatch.setattr(backup_mod.sys, "argv", ["backup"])

    backup_mod.main()

    assert seen_keep == [2]
    capsys.readouterr()  # swallow the printed path


def test_if_exists_skips_only_a_missing_database(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(
        backup_mod,
        "get_settings",
        lambda: _fake_settings("postgresql+psycopg://ea:pw@db.lan:5432/expenses", tmp_path),
    )

    def missing_db(*args, **kwargs):
        raise BackupError('connection failed: FATAL:  database "expenses" does not exist')

    monkeypatch.setattr(backup_mod, "create_pg_backup", missing_db)
    monkeypatch.setattr(backup_mod.sys, "argv", ["backup", "--if-exists"])

    backup_mod.main()  # clean skip, no SystemExit

    assert capsys.readouterr().out == ""  # empty stdout = deploy skips the backup


def test_if_exists_does_not_mask_a_missing_role(tmp_path: Path, monkeypatch):
    # A mistyped role also says "does not exist" — but the database may be
    # real and full; the deploy must ABORT, not skip its one safety backup.
    monkeypatch.setattr(
        backup_mod,
        "get_settings",
        lambda: _fake_settings("postgresql+psycopg://ea:pw@db.lan:5432/expenses", tmp_path),
    )

    def missing_role(*args, **kwargs):
        raise BackupError('connection failed: FATAL:  role "ea" does not exist')

    monkeypatch.setattr(backup_mod, "create_pg_backup", missing_role)
    monkeypatch.setattr(backup_mod.sys, "argv", ["backup", "--if-exists"])

    with pytest.raises(SystemExit):
        backup_mod.main()
