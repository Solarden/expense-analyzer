"""HTML-form input models for the dashboard.

These are the **presentation** layer's view of a submitted form: fields arrive as
the user typed them (money/rate as text, dates as ISO strings), so they're plain
strings here. A route handler parses and validates them into the domain models in
:mod:`expense_analyzer.models` (e.g. :class:`~expense_analyzer.models.LoanCreate`)
— keeping "raw text from the browser" separate from "validated domain types".

One deliberate exception: the CSV upload form stays as inline parameters in its
route, because it carries an ``UploadFile`` that doesn't belong in a plain model.
"""

from pydantic import BaseModel

from expense_analyzer.models import AccountType, CategoryKind, InstallmentType, RateType, Scope


class LoginForm(BaseModel):
    username: str
    password: str


class AccountForm(BaseModel):
    name: str
    type: AccountType


class CategoryForm(BaseModel):
    name: str
    kind: CategoryKind


class UserForm(BaseModel):
    username: str
    name: str
    password: str


class CategorizeForm(BaseModel):
    """A digit string selects that category; "" clears it (uncategorized)."""

    category_id: str = ""
    scope: Scope
    return_to: str = ""


class TransferConfirmForm(BaseModel):
    tx_a_id: int
    tx_b_id: int


class RateChangeForm(BaseModel):
    effective_date: str  # ISO date (YYYY-MM-DD)
    base_rate_percent: str  # % per year -> basis points


class PaymentLinkForm(BaseModel):
    tx_id: int
    installment_index: int


class LoanForm(BaseModel):
    """Raw loan-creation form fields. Amounts/rates arrive as the user typed them
    (PLN / percent strings) and the date as ISO text; the handler parses and
    validates them into a :class:`~expense_analyzer.models.LoanCreate`."""

    account_id: int
    principal: str  # PLN, e.g. "300000"
    rate_type: RateType
    rate_percent: str  # % per year; fixed: the rate, variable: the margin
    installment_type: InstallmentType
    start_date: str  # ISO date (YYYY-MM-DD)
    term_months: int
    base_rate_ref: str = ""
    base_rate_percent: str = ""  # variable only: initial base rate
