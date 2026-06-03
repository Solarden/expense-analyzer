"""Auth tests: hashing, login/logout, route protection, user management."""

from fastapi import status
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from expense_analyzer.auth import hash_password, verify_password
from expense_analyzer.models import Owner
from expense_analyzer.queries import users


def test_hash_verify_roundtrip():
    h = hash_password("s3cret")
    assert h != "s3cret"  # never store plaintext
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)


def test_dashboard_requires_login(client: TestClient, db_session: Session):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"] == "/login"


def test_login_success_sets_session(client: TestClient, db_session: Session):
    users.create_user(db_session, username="ada", name="Ada", password="lovelace")

    resp = client.post(
        "/login",
        data={"username": "ada", "password": "lovelace"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"] == "/dashboard"
    # Now the protected page is reachable.
    assert client.get("/dashboard").status_code == status.HTTP_200_OK


def test_login_wrong_password_401(client: TestClient, db_session: Session):
    users.create_user(db_session, username="ada", name="Ada", password="lovelace")

    resp = client.post(
        "/login",
        data={"username": "ada", "password": "nope"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Invalid username or password" in resp.text
    # Still locked out.
    assert client.get("/dashboard", follow_redirects=False).status_code == status.HTTP_303_SEE_OTHER


def test_login_unknown_user_401(client: TestClient, db_session: Session):
    resp = client.post(
        "/login",
        data={"username": "ghost", "password": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_logout_clears_session(auth_client: TestClient, db_session: Session):
    assert auth_client.get("/dashboard").status_code == status.HTTP_200_OK

    resp = auth_client.post("/logout", follow_redirects=False)
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"] == "/login"
    # After logout the dashboard redirects to login again.
    assert (
        auth_client.get("/dashboard", follow_redirects=False).status_code
        == status.HTTP_303_SEE_OTHER
    )


def test_inactive_user_is_logged_out(auth_client: TestClient, db_session: Session):
    # Deactivating the logged-in user invalidates their session immediately.
    tester = users.get_by_username(db_session, "tester")
    tester.is_active = False
    db_session.add(tester)
    db_session.commit()

    assert (
        auth_client.get("/dashboard", follow_redirects=False).status_code
        == status.HTTP_303_SEE_OTHER
    )


def test_users_page_can_add_user(auth_client: TestClient, db_session: Session):
    resp = auth_client.post(
        "/dashboard/users",
        data={"username": "bob", "name": "Bob", "password": "hunter2"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER

    bob = users.get_by_username(db_session, "bob")
    assert bob is not None
    assert bob.password_hash != "hunter2"
    assert verify_password("hunter2", bob.password_hash)


def test_users_page_rejects_duplicate_username(auth_client: TestClient, db_session: Session):
    # "tester" already exists (created by the auth_client fixture).
    resp = auth_client.post(
        "/dashboard/users",
        data={"username": "tester", "name": "Dup", "password": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_409_CONFLICT
    assert "already taken" in resp.text
    assert len(db_session.exec(select(Owner).where(Owner.username == "tester")).all()) == 1
