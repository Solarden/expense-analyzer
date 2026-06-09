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


# --- Admin role + user management (Phase 15) -------------------------------


def test_first_user_is_admin_rest_are_not(db_session: Session):
    first = users.create_user(db_session, username="root", name="Root", password="pw")
    second = users.create_user(db_session, username="member", name="Member", password="pw")

    assert first.is_admin is True
    assert second.is_admin is False


def test_user_added_via_ui_is_not_admin(auth_client: TestClient, db_session: Session):
    # auth_client is logged in as "tester" — the first user, hence admin.
    auth_client.post(
        "/dashboard/users",
        data={"username": "bob", "name": "Bob", "password": "hunter2"},
        follow_redirects=False,
    )
    bob = users.get_by_username(db_session, "bob")
    assert bob.is_admin is False


def _login_as(client: TestClient, username: str, password: str) -> None:
    resp = client.post(
        "/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER


def test_non_admin_cannot_manage_users(client: TestClient, db_session: Session):
    admin = users.create_user(db_session, username="admin", name="Admin", password="pw")
    users.create_user(db_session, username="plain", name="Plain", password="pw")
    _login_as(client, "plain", "pw")

    resp = client.post(f"/dashboard/users/{admin.id}/delete", follow_redirects=False)
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    # The admin is untouched.
    assert users.get(db_session, admin.id) is not None


def test_admin_can_deactivate_and_reactivate_member(client: TestClient, db_session: Session):
    users.create_user(db_session, username="admin", name="Admin", password="pw")
    member = users.create_user(db_session, username="plain", name="Plain", password="pw")
    _login_as(client, "admin", "pw")

    client.post(f"/dashboard/users/{member.id}/toggle-active", follow_redirects=False)
    db_session.refresh(member)
    assert member.is_active is False

    client.post(f"/dashboard/users/{member.id}/toggle-active", follow_redirects=False)
    db_session.refresh(member)
    assert member.is_active is True


def test_admin_can_delete_member_keeping_their_data(client: TestClient, db_session: Session):
    from expense_analyzer.models import Account, AccountType

    users.create_user(db_session, username="admin", name="Admin", password="pw")
    member = users.create_user(db_session, username="plain", name="Plain", password="pw")
    account = Account(name="Cash", type=AccountType.cash, owner_id=member.id)
    db_session.add(account)
    db_session.commit()
    member_id = member.id
    _login_as(client, "admin", "pw")

    resp = client.post(f"/dashboard/users/{member_id}/delete", follow_redirects=False)
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    # Drop the test session's identity-map cache so we read what the request committed.
    db_session.expire_all()
    assert users.get(db_session, member_id) is None
    # The imported data survives; only the "who imported" tag is cleared.
    db_session.refresh(account)
    assert account.owner_id is None


def test_admin_cannot_delete_self(client: TestClient, db_session: Session):
    admin = users.create_user(db_session, username="admin", name="Admin", password="pw")
    _login_as(client, "admin", "pw")

    resp = client.post(f"/dashboard/users/{admin.id}/delete", follow_redirects=False)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "your own account" in resp.text
    assert users.get(db_session, admin.id) is not None


def test_admin_cannot_deactivate_self(client: TestClient, db_session: Session):
    admin = users.create_user(db_session, username="admin", name="Admin", password="pw")
    _login_as(client, "admin", "pw")

    resp = client.post(f"/dashboard/users/{admin.id}/toggle-active", follow_redirects=False)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    db_session.refresh(admin)
    assert admin.is_active is True


def test_users_page_shows_manage_only_for_admin(client: TestClient, db_session: Session):
    users.create_user(db_session, username="admin", name="Admin", password="pw")
    users.create_user(db_session, username="plain", name="Plain", password="pw")

    _login_as(client, "admin", "pw")
    assert "Deactivate" in client.get("/dashboard/users").text

    client.post("/logout")
    _login_as(client, "plain", "pw")
    assert "Deactivate" not in client.get("/dashboard/users").text


# --- Phase 20a: password reset + admin toggle from the UI ---


def test_admin_can_reset_member_password(client: TestClient, db_session: Session):
    users.create_user(db_session, username="admin", name="Admin", password="pw")
    member = users.create_user(db_session, username="plain", name="Plain", password="oldpw")
    _login_as(client, "admin", "pw")

    resp = client.post(
        f"/dashboard/users/{member.id}/reset-password",
        data={"password": "newpw"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    # The new password works and the old one no longer does.
    db_session.refresh(member)
    assert verify_password("newpw", member.password_hash)
    assert not verify_password("oldpw", member.password_hash)


def test_reset_password_rejects_empty(client: TestClient, db_session: Session):
    users.create_user(db_session, username="admin", name="Admin", password="pw")
    member = users.create_user(db_session, username="plain", name="Plain", password="oldpw")
    _login_as(client, "admin", "pw")

    resp = client.post(
        f"/dashboard/users/{member.id}/reset-password",
        data={"password": "   "},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    # Password is untouched.
    db_session.refresh(member)
    assert verify_password("oldpw", member.password_hash)


def test_non_admin_cannot_reset_password(client: TestClient, db_session: Session):
    admin = users.create_user(db_session, username="admin", name="Admin", password="pw")
    users.create_user(db_session, username="plain", name="Plain", password="pw")
    _login_as(client, "plain", "pw")

    resp = client.post(
        f"/dashboard/users/{admin.id}/reset-password",
        data={"password": "hijack"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    db_session.refresh(admin)
    assert not verify_password("hijack", admin.password_hash)


def test_admin_can_grant_and_revoke_admin(client: TestClient, db_session: Session):
    users.create_user(db_session, username="admin", name="Admin", password="pw")
    member = users.create_user(db_session, username="plain", name="Plain", password="pw")
    _login_as(client, "admin", "pw")

    client.post(f"/dashboard/users/{member.id}/toggle-admin", follow_redirects=False)
    db_session.refresh(member)
    assert member.is_admin is True

    client.post(f"/dashboard/users/{member.id}/toggle-admin", follow_redirects=False)
    db_session.refresh(member)
    assert member.is_admin is False


def test_cannot_revoke_last_active_admin(client: TestClient, db_session: Session):
    admin = users.create_user(db_session, username="admin", name="Admin", password="pw")
    _login_as(client, "admin", "pw")

    # Sole admin tries to step down — blocked so the household keeps an admin.
    resp = client.post(f"/dashboard/users/{admin.id}/toggle-admin", follow_redirects=False)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    db_session.refresh(admin)
    assert admin.is_admin is True


def test_admin_can_step_down_when_another_admin_exists(client: TestClient, db_session: Session):
    admin = users.create_user(db_session, username="admin", name="Admin", password="pw")
    other = users.create_user(db_session, username="other", name="Other", password="pw")
    users.set_admin(db_session, other, is_admin=True)
    _login_as(client, "admin", "pw")

    resp = client.post(f"/dashboard/users/{admin.id}/toggle-admin", follow_redirects=False)
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    db_session.refresh(admin)
    assert admin.is_admin is False


def test_non_admin_cannot_toggle_admin(client: TestClient, db_session: Session):
    admin = users.create_user(db_session, username="admin", name="Admin", password="pw")
    users.create_user(db_session, username="plain", name="Plain", password="pw")
    _login_as(client, "plain", "pw")

    resp = client.post(f"/dashboard/users/{admin.id}/toggle-admin", follow_redirects=False)
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    db_session.refresh(admin)
    assert admin.is_admin is True
