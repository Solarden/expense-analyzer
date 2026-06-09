"""Phase 17 — UI/UX polish pass. HTTP-layer checks for the bits with server-side
behaviour: the transaction page-size picker, the net-worth assets/liabilities
split, and a couple of render smokes (burger, dark-theme chart script).

The purely-client polish (touch-target heights, focus ring, loan progressive
disclosure JS) is CSS/JS and isn't asserted here beyond the markup hooks it needs.
"""

from collections.abc import Callable

from fastapi.testclient import TestClient

from expense_analyzer.models import Account, AccountType, Loan

# --- Page-size picker ------------------------------------------------------


def _make_n_transactions(make_transaction: Callable[..., object], account: Account, n: int) -> None:
    for i in range(n):
        make_transaction(account_id=account.id, amount=-100 - i, raw_description=f"row {i}")


def test_page_size_picker_windows_to_chosen_size(
    auth_client: TestClient, make_transaction: Callable[..., object], account: Account
):
    _make_n_transactions(make_transaction, account, 30)

    resp = auth_client.get("/dashboard/transactions?size=25")
    assert resp.status_code == 200
    body = resp.text
    # 30 rows at 25/page → two pages (the default 50 would be a single page).
    assert "Page 1 of 2" in body
    assert "30 transactions" in body
    # The chosen size is sticky in the picker and carried by the pager links.
    assert '<option value="25" selected>25 / page</option>' in body
    assert "size=25" in body


def test_page_size_off_whitelist_falls_back_to_default(
    auth_client: TestClient, make_transaction: Callable[..., object], account: Account
):
    _make_n_transactions(make_transaction, account, 30)

    resp = auth_client.get("/dashboard/transactions?size=999")
    assert resp.status_code == 200
    body = resp.text
    # 999 isn't whitelisted → default 50 → one page, and the bogus value is not
    # echoed into the pager querystring.
    assert "Page 1 of 1" in body
    assert '<option value="50" selected>50 / page</option>' in body
    # The off-list value is not echoed into the pager querystring — the Next link
    # is a bare ?page=N (with a size it would read ?size=...&page=N).
    assert 'href="?page=2"' in body


# --- Net worth: assets / liabilities split ---------------------------------


def test_net_worth_assets_only_is_debt_free(
    auth_client: TestClient, make_transaction: Callable[..., object], account: Account
):
    make_transaction(account_id=account.id, amount=100_000)  # +1000 PLN asset

    resp = auth_client.get("/dashboard/net-worth")
    assert resp.status_code == 200
    body = resp.text
    assert "Assets" in body
    assert "Net worth excl. loans" in body
    # Asset chart present, liabilities side shows the debt-free message.
    assert 'id="assetsChart"' in body
    assert "No liabilities — debt-free." in body
    assert 'id="liabilitiesChart"' not in body


def test_net_worth_with_loan_shows_liabilities(
    auth_client: TestClient,
    make_transaction: Callable[..., object],
    make_account: Callable[..., Account],
    make_loan: Callable[..., Loan],
):
    bank = make_account(name="PKO", type=AccountType.bank)
    make_transaction(account_id=bank.id, amount=100_000)
    loan_account = make_account(name="Mortgage", type=AccountType.loan)
    make_loan(account_id=loan_account.id)

    resp = auth_client.get("/dashboard/net-worth")
    assert resp.status_code == 200
    body = resp.text
    # A loan contributes a liability, so the liabilities chart renders and the
    # debt-free message is gone.
    assert 'id="liabilitiesChart"' in body
    assert "No liabilities — debt-free." not in body


# --- Render smokes ---------------------------------------------------------


def test_base_renders_burger_and_nav_id(auth_client: TestClient):
    resp = auth_client.get("/dashboard/transactions")
    assert resp.status_code == 200
    assert 'class="nav-toggle' in resp.text
    assert 'id="primary-nav"' in resp.text
    # Escape-to-close handler (a11y follow-up) ships in the base layout.
    assert "if (e.key !== 'Escape') return;" in resp.text


def test_overview_includes_dark_chart_theme_script(auth_client: TestClient):
    resp = auth_client.get("/dashboard/stats")
    assert resp.status_code == 200
    assert "/static/chart-theme.js" in resp.text


def test_loan_form_has_variable_disclosure_hook(
    auth_client: TestClient, make_account: Callable[..., Account]
):
    make_account(name="Mortgage", type=AccountType.loan)

    resp = auth_client.get("/dashboard/loans")
    assert resp.status_code == 200
    assert 'id="variable-only"' in resp.text
    assert "/static/loan-form.js" in resp.text
