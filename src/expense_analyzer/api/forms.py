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


class TxDirection(StrEnum):
    """How a manually-entered amount maps to a signed amount. The user types a
    positive magnitude and picks a direction — clearer than a hand-typed minus,
    where a forgotten sign would record a cash expense as income."""

    expense = "expense"  # -> negative amount
    income = "income"  # -> positive amount


class LoginForm(BaseModel):
    username: str
    password: SecretStr  # masked in repr/logs; read via .get_secret_value()


class AccountForm(BaseModel):
    name: str
    type: AccountType


class CategoryForm(BaseModel):
    name: str
    kind: CategoryKind
    color: str = ""  # "#rrggbb" from <input type="color">, or "" for no colour


class CategoryColorForm(BaseModel):
    """Set one category's colour from the inline picker. ``color`` is a "#rrggbb"
    string; ``clear`` (a present submit button) wins and resets it to none."""

    color: str = ""
    clear: str = ""


class UserForm(BaseModel):
    username: str
    name: str
    password: SecretStr  # masked in repr/logs; read via .get_secret_value()


class CategorizeForm(BaseModel):
    """A digit string selects that category; "" clears it (uncategorized)."""

    category_id: str = ""
    scope: Scope
    return_to: str = ""


class ManualTransactionForm(BaseModel):
    """Hand-entered transaction (mainly cash). ``amount`` is a positive PLN string;
    ``direction`` gives it a sign. ``category_id`` is a digit string or "" (none)."""

    account_id: int
    booked_date: date  # ISO date from <input type="date">; Pydantic parses it
    amount: str  # positive PLN, e.g. "19,99"
    direction: TxDirection = TxDirection.expense
    description: str
    category_id: str = ""
    scope: Scope = Scope.private
    note: str = ""
    return_to: str = ""


class NoteForm(BaseModel):
    """The note-modal form: just the free-text note plus where to return."""

    note: str = ""
    return_to: str = ""


class EditTransactionForm(BaseModel):
    """Edit form. Category/scope/note apply to every row; the money fields are only
    read for manual entries (an imported row's amount/date/description are the
    bank's source of truth — the handler ignores them there)."""

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
    """Pick the portfolio account to import investment positions into."""

    account_id: int


class RateChangeForm(BaseModel):
    effective_date: date  # ISO date from <input type="date">; Pydantic parses it
    base_rate_percent: str  # % per year -> basis points (parsed via parse_pln)


class PaymentLinkForm(BaseModel):
    tx_id: int
    installment_index: int


class BudgetForm(BaseModel):
    """Raw budget form fields. ``limit_amount`` arrives as a PLN string the
    handler parses into minor units; ``month`` is ``""`` for the recurring default
    or a ``"YYYY-MM"`` override (validated in the handler)."""

    category_id: int
    month: str = ""  # "" -> recurring default; else "YYYY-MM" override
    limit_amount: str  # PLN, e.g. "2000"


class SubscriptionVerdictForm(BaseModel):
    """The merchant whose detected subscription a confirm/dismiss/restore acts on
    (the grouping key — see :class:`~expense_analyzer.models.Subscription`)."""

    merchant: str


class RuleForm(BaseModel):
    """Raw categorization-rule form fields. ``pattern`` is matched case-insensitively
    as a substring; ``priority`` orders the rules (higher wins)."""

    pattern: str
    category_id: int
    priority: int = 0


class LoanForm(BaseModel):
    """Raw loan-creation form fields. Amounts/rates arrive as the user typed them
    (PLN / percent strings) and the date as ISO text; the handler parses and
    validates them into a :class:`~expense_analyzer.models.LoanCreate`."""

    account_id: int
    principal: str  # PLN, e.g. "300000"
    rate_type: RateType
    rate_percent: str  # % per year; fixed: the rate, variable: the margin
    installment_type: InstallmentType
    start_date: date  # ISO date from <input type="date">; Pydantic parses it
    term_months: int
    base_rate_ref: str = ""
    base_rate_percent: str = ""  # variable only: initial base rate
