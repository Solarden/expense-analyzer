"""Seed a local SQLite database with a realistic demo dataset to click through.

Dev tool only — it REFUSES to run against a server database (production is the
shared Postgres; with its URL in .env a bare `make seed` would otherwise wipe
real finance data over the LAN). Idempotent: it wipes every data
table (keeping login users) and rebuilds a fresh household: a few accounts, a
category tree, ~4 months of transactions, a variable-rate mortgage with a rate
change, an investment snapshot, budgets, planned cashflow items, rules, and an
internal transfer each month. It goes through the real queries/ layer so the
data matches what the app itself would produce.

    uv run python -m scripts.seed_demo        # reseed (resets the 'admin' password)

Login afterwards: admin / demo1234
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import delete
from sqlmodel import Session, select

from expense_analyzer.clock import utc_now
from expense_analyzer.config import get_settings
from expense_analyzer.db import get_engine
from expense_analyzer.ha.update_notify import UpdateStatus, save_status
from expense_analyzer.models import (
    Account,
    AccountType,
    Budget,
    CategoryKind,
    ImportBatch,
    InstallmentType,
    InvestmentPosition,
    Loan,
    LoanCreate,
    LoanDocument,
    LoanRateChange,
    Owner,
    PlannedItem,
    PlannedItemPayment,
    RateType,
    Rule,
    Scope,
    Subscription,
    Transaction,
)
from expense_analyzer.queries.categorize import categories, rules
from expense_analyzer.queries.core import accounts, users
from expense_analyzer.queries.money import transactions, transfers
from expense_analyzer.queries.planning import budgets, loans, planned

DEMO_USERNAME = "admin"
DEMO_PASSWORD = "demo1234"  # nosec B105 - demo seed login, dev-only (see module docstring)


def z(pln: float) -> int:
    """PLN -> integer minor units (grosze). Demo helper; production never floats."""
    return int(round(pln * 100))


def wipe(session: Session) -> None:
    """Clear every data table, keeping the login users (owner)."""
    for model in (
        PlannedItemPayment,
        PlannedItem,
        LoanDocument,
        LoanRateChange,
        Loan,
        Budget,
        Subscription,
        Rule,
        InvestmentPosition,
        Transaction,
        ImportBatch,
        Account,
    ):
        session.exec(delete(model))
    # Categories last (transactions FK them).
    from expense_analyzer.models import Category

    session.exec(delete(Category))
    session.commit()


def ensure_login(session: Session) -> Owner:
    user = users.get_by_username(session, DEMO_USERNAME)
    if user is None:
        user = users.create_user(
            session, username=DEMO_USERNAME, name="Demo Admin", password=DEMO_PASSWORD
        )
    else:
        users.set_password(session, user, password=DEMO_PASSWORD)
        users.set_admin(session, user, is_admin=True)

    return user


def main() -> None:
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        raise SystemExit(
            f"refusing to seed a non-sqlite database ({engine.dialect.name}) — "
            "this wipes every data table. Point EA_DATABASE_URL at a local "
            "sqlite file to use the demo seed."
        )

    with Session(engine) as session:
        wipe(session)
        owner = ensure_login(session)

        # --- Accounts -------------------------------------------------------
        checking = accounts.create_account(session, name="PKO Checking", type=AccountType.bank)
        savings = accounts.create_account(session, name="PKO Savings", type=AccountType.bank)
        cash = accounts.create_account(session, name="Cash", type=AccountType.cash)
        portfolio = accounts.create_account(
            session, name="XTB Portfolio", type=AccountType.portfolio
        )
        mortgage_acct = accounts.create_account(session, name="Mortgage", type=AccountType.loan)

        # --- Categories -----------------------------------------------------
        exp = CategoryKind.expense
        inc = CategoryKind.income
        salary = categories.create_category(session, name="Salary", kind=inc, color="#3fb950")
        groceries = categories.create_category(session, name="Groceries", kind=exp, color="#58a6ff")
        dining = categories.create_category(session, name="Dining out", kind=exp, color="#f778ba")
        transport = categories.create_category(session, name="Transport", kind=exp, color="#d29922")
        utilities = categories.create_category(session, name="Utilities", kind=exp, color="#a371f7")
        entertainment = categories.create_category(
            session, name="Entertainment", kind=exp, color="#ff7b72"
        )
        health = categories.create_category(session, name="Health", kind=exp, color="#39c5cf")
        shopping = categories.create_category(session, name="Shopping", kind=exp, color="#db61a2")

        # --- Transactions: 4 months (Mar–Jun 2026) -------------------------
        def tx(account, d, amount, desc, cat, scope=Scope.private, note=None):
            return transactions.create_manual_transaction(
                session,
                account_id=account.id,
                booked_date=d,
                amount=amount,
                description=desc,
                category_id=cat,
                scope=scope,
                note=note,
                owner_id=owner.id,
            )

        # Imported (source=import_csv) + uncategorized rows so "Apply rules now"
        # demonstrably tags them — apply_rules never touches a human's manual row.
        from expense_analyzer.importers.merchant import normalize_merchant

        demo_batch = ImportBatch(source="PKO csv", filename="demo_2026.csv")
        session.add(demo_batch)
        session.commit()
        session.refresh(demo_batch)
        _seq = {"n": 0}

        def imp(account, d, amount, desc, scope=Scope.private):
            _seq["n"] += 1
            row = Transaction(
                account_id=account.id,
                import_batch_id=demo_batch.id,
                amount=amount,
                booked_date=d,
                raw_description=desc,
                merchant_normalized=normalize_merchant(desc),
                scope=scope,
                owner_id=owner.id,
                fingerprint=f"demo-{_seq['n']:04d}",
            )
            session.add(row)
            demo_batch.record_count += 1
            session.add(demo_batch)
            session.commit()

            return row

        months = [(2026, 3), (2026, 4), (2026, 5), (2026, 6)]
        for yr, mo in months:
            # Income
            tx(
                checking,
                date(yr, mo, 10),
                z(12000),
                "ACME Sp. z o.o. salary",
                salary.id,
                Scope.household,
            )
            # Recurring subscriptions (drive subscription detection) — imported &
            # uncategorized so "Apply rules" demonstrably tags them.
            imp(checking, date(yr, mo, 8), z(-43.00), "Netflix.com")
            imp(checking, date(yr, mo, 12), z(-19.99), "Spotify P12345")
            # Utilities (variable)
            tx(
                checking,
                date(yr, mo, 15),
                z(-(280 + mo * 7)),
                "Tauron energy",
                utilities.id,
                Scope.household,
            )
            # Groceries — a few per month, some uncategorized for the rules demo
            imp(checking, date(yr, mo, 3), z(-214.30), "Biedronka 1234", Scope.household)
            tx(checking, date(yr, mo, 11), z(-176.90), "Lidl Krakow", groceries.id, Scope.household)
            imp(checking, date(yr, mo, 22), z(-198.40), "Biedronka 1234", Scope.household)
            # Dining
            tx(checking, date(yr, mo, 7), z(-58.00), "Pod Wawelem restaurant", dining.id)
            tx(checking, date(yr, mo, 19), z(-34.50), "Uber Eats", dining.id)
            # Transport — uncategorized for rules
            imp(checking, date(yr, mo, 5), z(-260.00), "Orlen S.A. fuel")
            imp(cash, date(yr, mo, 17), z(-12.00), "Uber trip")
            # Entertainment / shopping / health, varied
            tx(
                checking,
                date(yr, mo, 14),
                z(-89.99),
                "Empik",
                shopping.id,
                note="Birthday gift for mum",
            )
            tx(checking, date(yr, mo, 24), z(-120.00), "Apteka Dbam o Zdrowie", health.id)
            tx(
                checking,
                date(yr, mo, 27),
                z(-49.00),
                "Multikino tickets",
                entertainment.id,
                note="Date night — splurge, skip next month",
            )

            # Internal transfer checking -> savings (autolinked below)
            tx(checking, date(yr, mo, 28), z(-2000), "Own transfer to savings", None)
            tx(savings, date(yr, mo, 28), z(2000), "Own transfer from checking", None)

        # --- Categorization rules ------------------------------------------
        rules.create_rule(session, pattern="Netflix", category_id=entertainment.id, priority=10)
        rules.create_rule(session, pattern="Spotify", category_id=entertainment.id, priority=10)
        rules.create_rule(session, pattern="Biedronka", category_id=groceries.id, priority=5)
        rules.create_rule(session, pattern="Lidl", category_id=groceries.id, priority=5)
        rules.create_rule(session, pattern="Orlen", category_id=transport.id, priority=5)
        rules.create_rule(session, pattern="Uber", category_id=transport.id, priority=1)
        applied = rules.apply_rules(session)

        # --- Internal transfer detection -----------------------------------
        linked, _ = transfers.detect_and_autolink(session, window_days=3)

        # --- Loan: variable-rate mortgage ----------------------------------
        loan = loans.create_loan(
            session,
            LoanCreate(
                account_id=mortgage_acct.id,
                principal=z(350_000),
                rate_type=RateType.variable,
                rate_bp=200,  # 2.00% margin
                base_rate_ref="WIBOR 3M",
                installment_type=InstallmentType.equal,
                start_date=date(2024, 1, 15),
                term_months=300,
                contract_number="BLP0068094260",
                initial_base_rate_bp=575,  # WIBOR 5.75% at disbursement
            ),
        )
        loans.add_rate_change(
            session, loan_id=loan.id, effective_date=date(2024, 10, 1), base_rate_bp=525
        )
        loans.add_rate_change(
            session, loan_id=loan.id, effective_date=date(2025, 6, 1), base_rate_bp=475
        )

        # --- Investment snapshot (XTB) -------------------------------------
        snapshot = date(2026, 6, 1)
        positions = [
            ("SXR8.DE", Decimal("12.5"), z(18_500), z(1_320_00 / 100), z(1_480_00 / 100), "EUR"),
            ("SNT.PL", Decimal("40"), z(6_240), z(140_00 / 100), z(156_00 / 100), "PLN"),
            ("CSPX.UK", Decimal("3.1980"), z(7_910), z(2_300_00 / 100), z(2_473_00 / 100), "USD"),
        ]
        for ticker, qty, value, avg, cur, ccy in positions:
            session.add(
                InvestmentPosition(
                    account_id=portfolio.id,
                    ticker=ticker,
                    quantity=qty,
                    value=value,
                    avg_price=avg,
                    current_price=cur,
                    currency=ccy,
                    snapshot_date=snapshot,
                    source="xtb",
                )
            )
        session.commit()

        # --- Budgets (recurring monthly limits) ----------------------------
        budgets.set_budget(session, category_id=groceries.id, month=None, limit_amount=z(2000))
        budgets.set_budget(session, category_id=dining.id, month=None, limit_amount=z(400))
        budgets.set_budget(session, category_id=transport.id, month=None, limit_amount=z(500))
        budgets.set_budget(session, category_id=entertainment.id, month=None, limit_amount=z(300))
        # A tight one-off override to show an "over budget" state in June.
        budgets.set_budget(session, category_id=shopping.id, month="2026-06", limit_amount=z(50))

        # --- Planned monthly cashflow items --------------------------------
        planned.create_planned_item(
            session, name="Salary", expected_amount=z(12000), category_id=salary.id, due_day=10
        )
        planned.create_planned_item(
            session,
            name="Mortgage",
            expected_amount=None,
            loan_id=loan.id,  # loan-backed: derived from the schedule
        )
        planned.create_planned_item(
            session,
            name="Utilities",
            expected_amount=None,  # variable
            category_id=utilities.id,
            due_day=15,
            payee_account="PL61109010140000071219812874",
        )
        planned.create_planned_item(
            session, name="Netflix", expected_amount=z(-43), category_id=entertainment.id, due_day=8
        )
        planned.create_planned_item(
            session,
            name="Spotify",
            expected_amount=z(-19.99),
            category_id=entertainment.id,
            due_day=12,
        )

        # Mark a couple of this month's manual items paid so the checklist isn't blank.
        this_month = "2026-06"
        for item in planned.list_planned_items(session):
            if item.name in {"Netflix", "Spotify"}:
                planned.mark_paid(session, planned_item_id=item.id, month=this_month)

        n_tx = len(session.exec(select(Transaction)).all())

    # Fake an "update available" verdict so System → Updates has something to show.
    # This is exactly the local file the cron check would write — no network here.
    save_status(
        UpdateStatus(current="v1.0.0", latest="v1.1.0", update_available=True),
        path=get_settings().update_status_path,
        checked_at=utc_now(),
    )

    print("Demo data seeded.")
    print(f"  transactions: {n_tx}  |  rules applied: {applied}  |  transfers linked: {linked}")
    print(f"  login: {DEMO_USERNAME} / {DEMO_PASSWORD}")


if __name__ == "__main__":
    main()
