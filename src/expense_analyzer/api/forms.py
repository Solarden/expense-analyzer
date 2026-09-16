"""HTML-form input models for the dashboard.

These are the **presentation** layer's view of a submitted form: fields arrive as
the user typed them (money/rate as text, dates as ISO strings), so they're plain
strings here. A route handler parses and validates them into the domain models in
:mod:`expense_analyzer.models` (e.g. :class:`~expense_analyzer.models.LoanCreate`)
— keeping "raw text from the browser" separate from "validated domain types".

One deliberate exception: the CSV upload form stays as inline parameters in its
route, because it carries an ``UploadFile`` that doesn't belong in a plain model.
"""

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, SecretStr

from expense_analyzer.models import AccountType, CategoryKind, InstallmentType, RateType, Scope


# The user types a positive magnitude and picks a direction, rather than typing a
# minus: a forgotten sign would silently record a cash expense as income.
class TxDirection(StrEnum):
    """Whether a manually-entered amount is an expense or income."""

    expense = "expense"  # -> negative amount
    income = "income"  # -> positive amount


class LoginForm(BaseModel):
    username: str
    password: SecretStr  # masked in repr/logs; read via .get_secret_value()


class AccountForm(BaseModel):
    # Used for both create and edit — the friendly ``name`` is what every picker
    # shows; ``number`` is the bank account number / IBAN (reference data, "" for none).
    # ``owner_id`` is "" for a shared account and a member id for a personal one,
    # which is what decides the scope of everything imported into it.
    name: str
    type: AccountType
    number: str = ""
    owner_id: str = ""


class CategoryForm(BaseModel):
    name: str
    kind: CategoryKind
    color: str = ""  # "#rrggbb" from <input type="color">, or "" for no colour


# `clear` is a present-only submit button; when present it wins over `color`.
class CategoryEditForm(BaseModel):
    """Rename a category, change its kind, and set or clear its colour."""

    name: str
    kind: CategoryKind
    color: str = ""
    clear: str = ""


class UserForm(BaseModel):
    username: str
    name: str
    password: SecretStr  # masked in repr/logs; read via .get_secret_value()


class PasswordResetForm(BaseModel):
    """A new password for a user, set by an administrator."""

    password: SecretStr  # masked in repr/logs; read via .get_secret_value()


class CategorizeForm(BaseModel):
    """The category to file a transaction under. Empty leaves it uncategorized."""

    category_id: str = ""
    scope: Scope
    return_to: str = ""


class ManualTransactionForm(BaseModel):
    """A transaction entered by hand, typically cash. The amount is a positive
    figure in PLN; the direction decides whether it counts as an expense or income."""

    account_id: int
    booked_date: date  # ISO date from <input type="date">; Pydantic parses it
    amount: str  # positive PLN, e.g. "19,99"
    direction: TxDirection = TxDirection.expense
    description: str
    category_id: str = ""
    # The fallback for a post that omits the field — a forgotten scope should not
    # create a row only its author can see. The template picks what is preselected.
    scope: Scope = Scope.household
    note: str = ""
    return_to: str = ""


class NoteForm(BaseModel):
    """A free-text note attached to a transaction."""

    note: str = ""
    return_to: str = ""


# An imported row's amount/date/description are the bank's source of truth, so the
# money fields are read only for manual entries.
class EditTransactionForm(BaseModel):
    """Changes to a transaction. Category, scope and note apply to any row; the
    amount, date, account and description apply only to manually-entered ones."""

    category_id: str = ""
    scope: Scope
    note: str = ""
    return_to: str = ""
    # manual-only (ignored for imported rows)
    account_id: int | None = None
    booked_date: date | None = None
    amount: str = ""
    direction: TxDirection = TxDirection.expense
    description: str = ""


class TransferConfirmForm(BaseModel):
    tx_a_id: int
    tx_b_id: int


class FetchPositionsForm(BaseModel):
    """The portfolio account to import investment positions into."""

    account_id: int


class RateChangeForm(BaseModel):
    effective_date: date  # ISO date from <input type="date">; Pydantic parses it
    base_rate_percent: str  # % per year -> basis points (parsed via parse_pln)


class PaymentLinkForm(BaseModel):
    tx_id: int
    installment_index: int


class BudgetForm(BaseModel):
    """A spending limit for a category: either the recurring default, or an
    override for a single month."""

    category_id: int
    month: str = ""  # "" -> recurring default; else "YYYY-MM" override
    limit_amount: str  # PLN, e.g. "2000"
    scope: Scope = Scope.household  # private (per-member) vs shared household limit


class SubscriptionVerdictForm(BaseModel):
    """The merchant whose detected subscription is being confirmed, dismissed or
    restored."""

    merchant: str


class RuleForm(BaseModel):
    """A rule that files matching transactions under a category. The pattern
    matches anywhere in the description, ignoring case; where several rules match,
    the highest priority wins."""

    pattern: str
    category_id: int
    priority: int = 0


# Amounts and rates arrive as the user typed them (PLN / percent strings) and are
# parsed and validated into a LoanCreate before anything is stored.
class LoanForm(BaseModel):
    """A new loan: its principal, interest rate, instalment style, start date and
    term."""

    account_id: int
    principal: str  # PLN, e.g. "300000"
    rate_type: RateType
    rate_percent: str  # % per year; fixed: the rate, variable: the margin
    installment_type: InstallmentType
    start_date: date  # ISO date from <input type="date">; Pydantic parses it
    term_months: int
    base_rate_ref: str = ""
    base_rate_percent: str = ""  # variable only: initial base rate
    contract_number: str = ""  # bank contract number, e.g. "BLP0068094260"


class PlannedItemForm(BaseModel):
    """A recurring planned income or expense. Leave the amount empty for a
    variable item with no fixed figure."""

    name: str
    amount: str = ""  # positive PLN magnitude; "" -> unestimated (variable)
    direction: TxDirection = TxDirection.expense
    category_id: str = ""  # digit string or "" (none)
    loan_id: str = ""  # digit string or "" -> loan-backed when set (amount derived)
    payee_account: str = ""
    due_day: str = ""  # "" or "1".."31"
    note: str = ""


class PlannedLinkForm(BaseModel):
    """Links a real transaction to a planned item for a given month."""

    tx_id: int
    month: str = ""
