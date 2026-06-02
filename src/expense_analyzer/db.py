from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from expense_analyzer.config import get_settings


@lru_cache
def get_engine() -> Engine:
    """Build (once) and return the SQLite engine.

    Lazy on purpose: importing this module must not open a connection, so the
    database path can still be overridden (e.g. by tests) before first use.
    """
    settings = get_settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        settings.database_url,
        echo=settings.debug,
        # SQLite + threaded server: allow connections across threads.
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        # WAL mode for write safety and better concurrency (see design doc §10).
        # foreign_keys is off by default in SQLite and must be enabled per-connection.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a database session."""
    with Session(get_engine()) as session:
        yield session
