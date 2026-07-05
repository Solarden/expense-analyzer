"""HTTP tests for the minimal Phase 1 dashboard.

Dashboard routes require login, so these use the ``auth_client`` fixture (a
TestClient already logged in as a freshly-created user). ``db_session`` creates
the schema on the same (temp) engine the app uses. Model builders and the shared
``fake_importer`` registry fixture come from conftest.
"""

from collections.abc import Callable

from fastapi import status
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from expense_analyzer.importers import registry as importer_registry
from expense_analyzer.models import Account, Category, CategoryKind, Transaction


def test_index_renders(auth_client: TestClient, db_session: Session):
    resp = auth_client.get("/dashboard")
    assert resp.status_code == status.HTTP_200_OK
    assert "Expense Analyzer" in resp.text


def test_dashboard_home_is_overview(auth_client: TestClient, db_session: Session):
    # /dashboard now lands on the Overview, not the account/category setup page.
    body = auth_client.get("/dashboard").text
    assert "Overview" in body
    assert "Add account" not in body


def test_settings_page_is_config(auth_client: TestClient, db_session: Session):
    # The setup page (accounts & categories) moved to /dashboard/settings.
    body = auth_client.get("/dashboard/settings").text
    assert "Add account" in body
    assert "Add category" in body


def test_create_account_then_listed(auth_client: TestClient, db_session: Session):
    resp = auth_client.post(
        "/dashboard/accounts",
        data={"name": "PKO checking", "type": "bank"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert auth_client.get("/dashboard/settings").text.count("PKO checking") >= 1


# --- Account number / IBAN (friendly name stays; number is reference data) ---

# A well-known valid Polish IBAN test value (passes the mod-97 checksum).
VALID_IBAN = "PL61109010140000071219812874"


def test_create_account_with_number(auth_client: TestClient, db_session: Session):
    # Typed with spaces and lower case — stored normalised, and shown on settings.
    typed = "pl61 1090 1014 0000 0712 1981 2874"
    auth_client.post(
        "/dashboard/accounts",
        data={"name": "PKO checking", "type": "bank", "number": typed},
    )
    acc = db_session.exec(select(Account)).one()
    assert acc.number == VALID_IBAN
    assert VALID_IBAN in auth_client.get("/dashboard/settings").text


def test_create_account_blank_number_is_none(auth_client: TestClient, db_session: Session):
    auth_client.post("/dashboard/accounts", data={"name": "Cash", "type": "cash", "number": "  "})
    assert db_session.exec(select(Account)).one().number is None


def test_create_account_non_iban_number_kept_as_typed(auth_client: TestClient, db_session: Session):
    # A brokerage/cash id isn't an IBAN — kept exactly as typed (case + separators),
    # only trimmed. We must not uppercase it or strip its internal characters.
    auth_client.post(
        "/dashboard/accounts",
        data={"name": "IKE XTB", "type": "portfolio", "number": "  xtb-Acc-123  "},
    )
    assert db_session.exec(select(Account)).one().number == "xtb-Acc-123"


def test_create_account_invalid_iban_rejected(auth_client: TestClient, db_session: Session):
    resp = auth_client.post(
        "/dashboard/accounts",
        data={"name": "PKO checking", "type": "bank", "number": "PL00109010140000071219812874"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert db_session.exec(select(Account)).all() == []  # nothing written


def test_create_account_blank_name_rejected(auth_client: TestClient, db_session: Session):
    # Boy-scout: create used to accept an all-whitespace name silently.
    resp = auth_client.post("/dashboard/accounts", data={"name": "   ", "type": "bank"})
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert db_session.exec(select(Account)).all() == []


def test_edit_account_renames_and_sets_number(
    auth_client: TestClient, db_session: Session, make_account: Callable[..., Account]
):
    acc = make_account(name="61 1090 1014")  # named by number before the rename
    auth_client.post(
        f"/dashboard/accounts/{acc.id}/edit",
        data={"name": "  PKO checking  ", "type": "bank", "number": VALID_IBAN},
        follow_redirects=False,
    )
    db_session.refresh(acc)
    assert acc.name == "PKO checking"  # whitespace trimmed
    assert acc.number == VALID_IBAN


def test_edit_account_unknown_404(auth_client: TestClient, db_session: Session):
    resp = auth_client.post(
        "/dashboard/accounts/9999/edit",
        data={"name": "PKO checking", "type": "bank"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_edit_account_empty_name_rejected(
    auth_client: TestClient, db_session: Session, make_account: Callable[..., Account]
):
    acc = make_account(name="PKO checking")
    resp = auth_client.post(
        f"/dashboard/accounts/{acc.id}/edit",
        data={"name": "   ", "type": "bank"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    db_session.refresh(acc)
    assert acc.name == "PKO checking"  # unchanged


def test_create_account_error_preserves_input(auth_client: TestClient, db_session: Session):
    # A bad IBAN is rejected; the name/number the user typed survive the re-render.
    resp = auth_client.post(
        "/dashboard/accounts",
        data={"name": "My Bank", "type": "bank", "number": "PL00109010140000071219812874"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert 'value="My Bank"' in resp.text
    assert 'value="PL00109010140000071219812874"' in resp.text
    assert db_session.exec(select(Account)).all() == []  # nothing written


def test_edit_account_error_preserves_input(
    auth_client: TestClient, db_session: Session, make_account: Callable[..., Account]
):
    # A bad IBAN on the inline edit form re-renders that row with the attempted edit.
    acc = make_account(name="Orig")
    resp = auth_client.post(
        f"/dashboard/accounts/{acc.id}/edit",
        data={"name": "Edited", "type": "bank", "number": "PL00109010140000071219812874"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert 'value="Edited"' in resp.text  # the attempted name is kept, not reset to "Orig"
    assert 'value="PL00109010140000071219812874"' in resp.text
    db_session.refresh(acc)
    assert acc.name == "Orig"  # DB unchanged


def test_create_category(auth_client: TestClient, db_session: Session):
    auth_client.post("/dashboard/categories", data={"name": "Food", "kind": "expense"})
    cats = db_session.exec(select(Category)).all()
    assert [c.name for c in cats] == ["Food"]
    assert cats[0].kind == CategoryKind.expense


# --- Phase 16: per-category colour ---


def test_create_category_with_colour(auth_client: TestClient, db_session: Session):
    auth_client.post(
        "/dashboard/categories",
        data={"name": "Food", "kind": "expense", "color": "#FF8800"},
    )
    cat = db_session.exec(select(Category)).one()
    assert cat.color == "#ff8800"  # normalised to lower-case


def test_create_category_blank_colour_is_none(auth_client: TestClient, db_session: Session):
    auth_client.post("/dashboard/categories", data={"name": "Food", "kind": "expense", "color": ""})
    assert db_session.exec(select(Category)).one().color is None


def test_create_category_invalid_colour_rejected(auth_client: TestClient, db_session: Session):
    resp = auth_client.post(
        "/dashboard/categories",
        data={"name": "Food", "kind": "expense", "color": "red"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert db_session.exec(select(Category)).all() == []  # nothing created on bad input


# --- Phase 20b: full category edit (rename / change kind / set-clear colour) ---


def test_edit_category_sets_colour(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
):
    cat = make_category(name="Food", kind=CategoryKind.expense)
    resp = auth_client.post(
        f"/dashboard/categories/{cat.id}/edit",
        data={"name": "Food", "kind": "expense", "color": "#3fb950"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER
    db_session.refresh(cat)
    assert cat.color == "#3fb950"


def test_edit_category_clears_colour(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
):
    cat = make_category(name="Food", kind=CategoryKind.expense, color="#3fb950")
    # The clear button wins even when a colour value is also submitted.
    auth_client.post(
        f"/dashboard/categories/{cat.id}/edit",
        data={"name": "Food", "kind": "expense", "color": "#3fb950", "clear": "1"},
        follow_redirects=False,
    )
    db_session.refresh(cat)
    assert cat.color is None


def test_edit_category_invalid_colour_rejected(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
):
    cat = make_category(name="Food", kind=CategoryKind.expense)
    resp = auth_client.post(
        f"/dashboard/categories/{cat.id}/edit",
        data={"name": "Food", "kind": "expense", "color": "nope"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    db_session.refresh(cat)
    assert cat.color is None


def test_edit_category_unknown_404(auth_client: TestClient, db_session: Session):
    resp = auth_client.post(
        "/dashboard/categories/9999/edit",
        data={"name": "Food", "kind": "expense", "color": "#3fb950"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_edit_category_renames(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
):
    cat = make_category(name="Food", kind=CategoryKind.expense)
    auth_client.post(
        f"/dashboard/categories/{cat.id}/edit",
        data={"name": "  Groceries  ", "kind": "expense"},  # whitespace is trimmed
        follow_redirects=False,
    )
    db_session.refresh(cat)
    assert cat.name == "Groceries"


def test_edit_category_changes_kind(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
):
    cat = make_category(name="Salary", kind=CategoryKind.expense)
    auth_client.post(
        f"/dashboard/categories/{cat.id}/edit",
        data={"name": "Salary", "kind": "income"},
        follow_redirects=False,
    )
    db_session.refresh(cat)
    assert cat.kind == CategoryKind.income


def test_edit_category_empty_name_rejected(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
):
    cat = make_category(name="Food", kind=CategoryKind.expense)
    resp = auth_client.post(
        f"/dashboard/categories/{cat.id}/edit",
        data={"name": "   ", "kind": "expense"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    db_session.refresh(cat)
    assert cat.name == "Food"  # unchanged


def test_edit_category_empty_name_wins_over_bad_colour(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
):
    # Name is the required field, so its error takes precedence (checked first).
    cat = make_category(name="Food", kind=CategoryKind.expense)
    resp = auth_client.post(
        f"/dashboard/categories/{cat.id}/edit",
        data={"name": "", "kind": "expense", "color": "nope"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    # Autoescape turns the apostrophe into "&#39;", so match the unambiguous tail.
    assert "name can" in resp.text and "be empty" in resp.text


def test_swatch_renders_for_coloured_category(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
):
    make_category(name="Food", kind=CategoryKind.expense, color="#abcdef")
    # The index Categories table renders the swatch for every coloured category.
    assert "background:#abcdef" in auth_client.get("/dashboard/settings").text


def test_no_swatch_for_colourless_category(
    auth_client: TestClient, db_session: Session, make_category: Callable[..., Category]
):
    make_category(name="Food", kind=CategoryKind.expense)
    # Colourless categories render no swatch span (the picker still defaults to one).
    assert 'class="swatch"' not in auth_client.get("/dashboard/settings").text


def test_upload_imports_transactions(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    fake_importer: str,
):
    resp = auth_client.post(
        "/dashboard/upload",
        data={"account_id": str(account.id), "importer": fake_importer},
        files={"file": ("may.csv", b"ignored-by-fake", "text/csv")},
    )
    assert resp.status_code == status.HTTP_200_OK
    assert "Imported" in resp.text

    rows = db_session.exec(select(Transaction)).all()
    assert len(rows) == 2
    assert {r.amount for r in rows} == {-12345, 1000000}


def test_categorize_sets_category_and_scope(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    make_category: Callable[..., Category],
    fake_importer: str,
):
    cat = make_category(name="Food", kind=CategoryKind.expense)

    auth_client.post(
        "/dashboard/upload",
        data={"account_id": str(account.id), "importer": fake_importer},
        files={"file": ("may.csv", b"x", "text/csv")},
    )
    tx = db_session.exec(select(Transaction)).first()

    resp = auth_client.post(
        f"/dashboard/transactions/{tx.id}/categorize",
        data={"category_id": str(cat.id), "scope": "household", "account_id": str(account.id)},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_303_SEE_OTHER

    db_session.refresh(tx)
    assert tx.category_id == cat.id
    assert tx.scope.value == "household"
    assert tx.source.value == "manual"


def test_rollback_hides_transactions_from_list(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    fake_importer: str,
):
    up = auth_client.post(
        "/dashboard/upload",
        data={"account_id": str(account.id), "importer": fake_importer},
        files={"file": ("may.csv", b"x", "text/csv")},
    )
    assert "Biedronka" in auth_client.get("/dashboard/transactions").text

    batch_id = db_session.exec(select(Transaction)).first().import_batch_id
    auth_client.post(f"/dashboard/batches/{batch_id}/rollback", follow_redirects=False)

    # Soft-deleted rows drop out of the transaction list.
    assert "Biedronka" not in auth_client.get("/dashboard/transactions").text
    assert up.status_code == status.HTTP_200_OK


def test_filter_params_tolerate_empty_and_garbage(auth_client: TestClient) -> None:
    """The filter bar auto-submits every '— all … —' option as an empty string, so
    empty/invalid query params must be ignored, never 422.

    Regression: changing the scope filter sent ``account_id=`` (the "all accounts"
    option) and FastAPI 422'd trying to parse "" as int. Empty + garbage values for
    every typed filter now resolve to "no filter".
    """
    # The exact reported case: scope picked, account left on "all accounts".
    reported = auth_client.get(
        "/dashboard/transactions",
        params={"account_id": "", "scope": "private", "month": "", "category": ""},
    )
    assert reported.status_code == status.HTTP_200_OK

    # Siblings: empty scope ("any"), empty page, and hand-edited garbage are ignored.
    garbage = auth_client.get(
        "/dashboard/transactions",
        params={"account_id": "abc", "scope": "nonsense", "page": ""},
    )
    assert garbage.status_code == status.HTTP_200_OK


def test_upload_unknown_account_shows_error(
    auth_client: TestClient,
    db_session: Session,
    fake_importer: str,
):
    resp = auth_client.post(
        "/dashboard/upload",
        data={"account_id": "999", "importer": fake_importer},
        files={"file": ("may.csv", b"x", "text/csv")},
    )
    assert resp.status_code == status.HTTP_200_OK
    assert "Unknown account" in resp.text
    assert len(db_session.exec(select(Transaction)).all()) == 0  # nothing imported


def test_upload_unknown_importer_shows_error(
    auth_client: TestClient, db_session: Session, account: Account
):
    resp = auth_client.post(
        "/dashboard/upload",
        data={"account_id": str(account.id), "importer": "nope"},
        files={"file": ("may.csv", b"x", "text/csv")},
    )
    assert resp.status_code == status.HTTP_200_OK
    assert "Unknown importer" in resp.text


def test_upload_unparseable_file_shows_error(
    auth_client: TestClient, db_session: Session, account: Account
):
    from expense_analyzer.importers.base import ImporterError

    class BoomImporter:
        source = "Boom csv"

        def parse(self, data: bytes):
            raise ImporterError("line 7: bad amount")

    importer_registry.register("boom", BoomImporter())
    try:
        resp = auth_client.post(
            "/dashboard/upload",
            data={"account_id": str(account.id), "importer": "boom"},
            files={"file": ("bad.csv", b"x", "text/csv")},
        )
        assert resp.status_code == status.HTTP_200_OK
        assert "Could not parse the file" in resp.text
        assert "line 7: bad amount" in resp.text
        assert len(db_session.exec(select(Transaction)).all()) == 0
    finally:
        importer_registry._REGISTRY.pop("boom", None)


def test_categorize_rejects_non_numeric_category(auth_client: TestClient, db_session: Session):
    resp = auth_client.post(
        "/dashboard/transactions/1/categorize",
        data={"category_id": "abc", "scope": "private"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_categorize_rejects_unknown_category(
    auth_client: TestClient,
    db_session: Session,
    account: Account,
    fake_importer: str,
):
    auth_client.post(
        "/dashboard/upload",
        data={"account_id": str(account.id), "importer": fake_importer},
        files={"file": ("may.csv", b"x", "text/csv")},
    )
    tx = db_session.exec(select(Transaction)).first()
    resp = auth_client.post(
        f"/dashboard/transactions/{tx.id}/categorize",
        data={"category_id": "9999", "scope": "private"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_categorize_unknown_transaction_404(auth_client: TestClient, db_session: Session):
    resp = auth_client.post(
        "/dashboard/transactions/12345/categorize",
        data={"category_id": "", "scope": "private"},
        follow_redirects=False,
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
