"""Investments and net-worth pages — endpoint smoke + behaviour tests."""

from collections.abc import Callable

from fastapi import status
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from expense_analyzer.models import Account, AccountType, InvestmentPosition


def test_investments_page_empty_prompts_for_portfolio(auth_client: TestClient) -> None:
    resp = auth_client.get("/dashboard/investments")
    assert resp.status_code == status.HTTP_200_OK
    assert "portfolio" in resp.text.lower()


def test_investments_page_lists_holdings(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_investment: Callable[..., InvestmentPosition],
) -> None:
    acc = make_account(name="IKE XTB", type=AccountType.portfolio)
    make_investment(account_id=acc.id, ticker="SNT.PL", value=900_00)

    resp = auth_client.get("/dashboard/investments")
    assert resp.status_code == status.HTTP_200_OK
    assert "IKE XTB" in resp.text
    assert "SNT.PL" in resp.text


def test_fetch_without_config_flashes_error(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
) -> None:
    acc = make_account(name="IKE XTB", type=AccountType.portfolio)

    resp = auth_client.post("/dashboard/investments/fetch", data={"account_id": acc.id})
    # myFund is not configured in tests -> a clear flash, not a 500.
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "not configured" in resp.text.lower()


def test_upload_xtb_imports_positions(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    xtb_xlsx: Callable[..., bytes],
) -> None:
    acc = make_account(name="IKE XTB", type=AccountType.portfolio)

    resp = auth_client.post(
        "/dashboard/investments/upload",
        data={"account_id": acc.id},
        files={"file": ("export.xlsx", xtb_xlsx(), "application/octet-stream")},
    )
    assert resp.status_code == status.HTTP_200_OK
    assert "Imported" in resp.text
    assert "SXR8.DE" in resp.text

    rows = db_session.exec(
        select(InvestmentPosition).where(InvestmentPosition.account_id == acc.id)
    ).all()
    assert {r.ticker for r in rows} == {"SXR8.DE", "SNT.PL"}


def test_upload_to_non_portfolio_account_rejected(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    xtb_xlsx: Callable[..., bytes],
) -> None:
    acc = make_account(name="PKO", type=AccountType.bank)

    resp = auth_client.post(
        "/dashboard/investments/upload",
        data={"account_id": acc.id},
        files={"file": ("export.xlsx", xtb_xlsx(), "application/octet-stream")},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "portfolio" in resp.text.lower()


def test_net_worth_page_renders(
    auth_client: TestClient,
    db_session: Session,
    make_account: Callable[..., Account],
    make_transaction: Callable[..., object],
    make_investment: Callable[..., InvestmentPosition],
) -> None:
    bank = make_account(name="PKO", type=AccountType.bank)
    make_transaction(account_id=bank.id, amount=1_000_00)
    portfolio = make_account(name="IKE", type=AccountType.portfolio)
    make_investment(account_id=portfolio.id, value=500_00)

    resp = auth_client.get("/dashboard/net-worth")
    assert resp.status_code == status.HTTP_200_OK
    assert "Net worth" in resp.text
    assert "PKO" in resp.text and "IKE" in resp.text
