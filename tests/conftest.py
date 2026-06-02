"""Shared test fixtures.

The database path is redirected to a throwaway temp file *at import time* —
before any test module imports the app (which builds the engine at import).
This keeps tests off the real ``data/`` database.
"""

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

# Must run before `expense_analyzer` is imported anywhere in the test session.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="ea-test-"))
os.environ.setdefault("EA_DATABASE_PATH", str(_TMP_DIR / "test.db"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlmodel import Session, SQLModel  # noqa: E402


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
    from expense_analyzer.db import engine

    SQLModel.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        SQLModel.metadata.drop_all(engine)
