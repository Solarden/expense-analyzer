"""Shared test fixtures.

The database path is redirected to a throwaway temp file so tests never touch
the real ``data/`` database. This is safe to do after imports because the
engine is built lazily (see expense_analyzer.db.get_engine) — nothing opens a
connection at import time.
"""

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel

from expense_analyzer.config import get_settings

os.environ["EA_DATABASE_PATH"] = str(Path(tempfile.mkdtemp(prefix="ea-test-")) / "test.db")
get_settings.cache_clear()  # drop any settings cached before the override


@pytest.fixture
def client() -> Iterator[TestClient]:
    from expense_analyzer.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_session() -> Iterator[Session]:
    """A session against a fresh schema. Tables are created before and dropped
    after each test, so model tests start from a clean slate.

    In Phase 0 there are no models yet, so this just exercises the engine."""
    from expense_analyzer.db import get_engine

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        SQLModel.metadata.drop_all(engine)
