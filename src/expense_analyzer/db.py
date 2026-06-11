from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine, make_url
from sqlmodel import Session, create_engine

from expense_analyzer.config import get_settings


@lru_cache
def get_engine() -> Engine:
    """Build (once) and return the database engine.

    Lazy on purpose: importing this module must not open a connection, so the
    database URL can still be overridden (e.g. by tests) before first use.
    Dialect-aware: SQLite for zero-setup local dev, PostgreSQL in production
    (the shared /opt/stack server — see docker-compose.yml).
    """
    settings = get_settings()
    url = make_url(settings.database_url)

    if url.get_backend_name() == "sqlite":
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)

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

    # Server database (PostgreSQL). Small fixed pool — app and worker each hold
    # one (5+5 max), well under the server's connection limit. pre_ping matters:
    # the server lives in a separate compose stack and can restart independently
    # of the app, so stale pooled connections must be detected, not crashed on.
    return create_engine(
        settings.database_url,
        echo=settings.debug,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
    )


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a database session."""
    with Session(get_engine()) as session:
        yield session
