"""The per-viewer visibility predicate — the segregation feature's security boundary.

Asserts the invariant (never leak another member's private row) and that each lens
partitions the visible set, including the safe ``viewer_id=None`` default.
"""

from collections.abc import Callable

from sqlmodel import Session, select

from expense_analyzer.models import Account, Lens, Scope, Transaction
from expense_analyzer.queries.core import users
from expense_analyzer.queries.visibility import resolve_lens, visible_to


def _visible_ids(session: Session, *, viewer_id: int | None, lens: Lens) -> set[int]:
    query = visible_to(select(Transaction), viewer_id=viewer_id, lens=lens)

    return {tx.id for tx in session.exec(query).all()}


def test_visible_to_hides_other_members_private(
    db_session: Session,
    account: Account,
    make_transaction: Callable[..., Transaction],
):
    alice = users.create_user(db_session, username="alice", name="Alice", password="pw")
    bob = users.create_user(db_session, username="bob", name="Bob", password="pw")
    a_priv = make_transaction(
        account_id=account.id, amount=-100, owner_id=alice.id, scope=Scope.private
    )
    b_priv = make_transaction(
        account_id=account.id, amount=-200, owner_id=bob.id, scope=Scope.private
    )
    shared = make_transaction(
        account_id=account.id, amount=-300, owner_id=alice.id, scope=Scope.household
    )

    # all: my private + all household — never bob's private.
    assert _visible_ids(db_session, viewer_id=alice.id, lens=Lens.all) == {a_priv.id, shared.id}
    # private: only my own private.
    assert _visible_ids(db_session, viewer_id=alice.id, lens=Lens.private) == {a_priv.id}
    # home: household only (any owner).
    assert _visible_ids(db_session, viewer_id=alice.id, lens=Lens.home) == {shared.id}
    # bob sees his own private + household, but never alice's private.
    assert _visible_ids(db_session, viewer_id=bob.id, lens=Lens.all) == {b_priv.id, shared.id}
    assert a_priv.id not in _visible_ids(db_session, viewer_id=bob.id, lens=Lens.all)
    # No viewer (background job) -> household only, never anyone's private.
    assert _visible_ids(db_session, viewer_id=None, lens=Lens.all) == {shared.id}
    assert _visible_ids(db_session, viewer_id=None, lens=Lens.private) == set()


def test_resolve_lens_defaults_safely():
    assert resolve_lens("private") is Lens.private
    assert resolve_lens("home") is Lens.home
    assert resolve_lens("all") is Lens.all
    assert resolve_lens(None) is Lens.all
    assert resolve_lens("bogus") is Lens.all
