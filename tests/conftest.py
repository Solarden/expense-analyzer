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
os.environ.setdefault("EA_SECRET_KEY", "test-secret-not-for-production")  # app refuses default
get_settings.cache_clear()  # drop any settings cached before the override


@pytest.fixture(autouse=True, scope="session")
def _fast_bcrypt() -> Iterator[None]:
    """Use minimal bcrypt rounds in tests — real cost (12) makes the suite slow."""
    import bcrypt

    original = bcrypt.gensalt
    bcrypt.gensalt = lambda rounds=4, prefix=b"2b": original(rounds=4, prefix=prefix)
    try:
        yield
    finally:
        bcrypt.gensalt = original


# Committed test fixtures (anonymized sample data). Resolved from this conftest
# so tests find them regardless of which subdirectory they live in.
FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def client() -> Iterator[TestClient]:
    from expense_analyzer.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client(client: TestClient, db_session: Session) -> TestClient:
    """A TestClient already logged in as a freshly-created user.

    The session cookie set by /login persists on the client, so subsequent
    requests hit the (auth-protected) dashboard as that user.
    """
    from expense_analyzer.queries import users

    users.create_user(db_session, username="tester", name="Tester", password="secret123")
    resp = client.post(
        "/login",
        data={"username": "tester", "password": "secret123"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    return client


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
