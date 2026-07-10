"""Natural-language spending queries (PR 4).

The owner types a question ("how much did I spend on groceries last month?"); the
LLM turns it into a *structured filter* — never SQL, never code — and this module
answers from the app's own data.

**Security is the point of the feature, and it lives in :func:`build_spec`.** The
LLM output is only a hint: every field is re-validated and resolved here before
use — category/account names → real ids (unknown dropped), dates via
``date.fromisoformat``, amounts coerced to int minor units, enums checked. Anything
unrecognized is ignored, not executed. Execution (:func:`run_query`) is pure-Python
filtering over the preloaded :func:`spendable_transactions` list, so there is no
dynamic query: a malformed or hostile spec can at worst produce an empty or broad
result, never an error or a data leak. Unparseable LLM output → a friendly
"couldn't interpret" result (:func:`answer`), never a 500.
"""

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date

from sqlmodel import Session

from expense_analyzer.clock import local_today
from expense_analyzer.config import Settings, get_settings
from expense_analyzer.models import Account, Category, Transaction
from expense_analyzer.ollama import OllamaClient, OllamaError
from expense_analyzer.queries.categorize.categories import list_categories
from expense_analyzer.queries.core.accounts import list_accounts
from expense_analyzer.queries.money.stats import (
    UNCATEGORIZED_LABEL,
    CategoryTotal,
    MonthTotals,
    spendable_transactions,
)

# Cap the read-only row list; the total and breakdown still cover every match.
_MAX_ROWS = 200

_DIRECTIONS = ("expense", "income")
_GROUP_BYS = ("category", "month")
_COULDNT_INTERPRET = "Sorry, I couldn't interpret that question. Try rephrasing it."


@dataclass(frozen=True)
class QuerySpec:
    """The validated, typed filter — the only thing :func:`run_query` ever sees."""

    category_id: int | None = None
    account_id: int | None = None
    start: date | None = None
    end: date | None = None
    min_amount: int | None = None  # minor units (grosze)
    max_amount: int | None = None  # minor units (grosze)
    direction: str | None = None  # "expense" | "income"
    group_by: str | None = None  # "category" | "month"


@dataclass(frozen=True)
class QueryResult:
    interpretation: str  # the model's restatement, or the disabled/failure message
    total: int  # sum of matched magnitudes, minor units
    rows: list[Transaction]  # newest-first, capped at _MAX_ROWS
    matched: int  # total matches (rows may be capped below this)
    breakdown: list[CategoryTotal] | list[MonthTotals]
    group_by: str | None
    ok: bool  # False when disabled or uninterpretable → the template shows a message


# --- validation boundary (pure; no LLM/DB) ---------------------------------


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_amount(value: object) -> int | None:
    """``12.34`` zł (or ``"12.34"``) → ``1234`` minor units. Garbage → ``None``.

    ``round`` not ``int``: ``int(0.29 * 100) == 28`` — a lost grosz.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        # json.loads accepts Infinity/NaN by default, so a hostile reply can reach
        # here: NaN -> ValueError, Infinity -> OverflowError. Both drop to None
        # rather than escaping build_spec and 500-ing the page.
        return round(float(value) * 100)
    except (TypeError, ValueError, OverflowError):
        return None


def build_spec(raw: dict, categories: list[Category], accounts: list[Account]) -> QuerySpec:
    """Resolve a raw LLM dict into a typed :class:`QuerySpec`, dropping anything
    unrecognized. Pure — no LLM, no DB. This is the security workhorse."""
    cat_by_name = {c.name.casefold(): c.id for c in categories if c.id is not None}
    acc_by_name = {a.name.casefold(): a.id for a in accounts if a.id is not None}

    category = raw.get("category")
    account = raw.get("account")
    direction = raw.get("direction")
    group_by = raw.get("group_by")

    return QuerySpec(
        category_id=cat_by_name.get(category.casefold()) if isinstance(category, str) else None,
        account_id=acc_by_name.get(account.casefold()) if isinstance(account, str) else None,
        start=_parse_date(raw.get("start_date")),
        end=_parse_date(raw.get("end_date")),
        min_amount=_parse_amount(raw.get("min_amount")),
        max_amount=_parse_amount(raw.get("max_amount")),
        direction=direction if direction in _DIRECTIONS else None,
        group_by=group_by if group_by in _GROUP_BYS else None,
    )


# --- execution (pure-Python filter over spendable_transactions) ------------


def _matches(tx: Transaction, spec: QuerySpec) -> bool:
    if spec.start is not None and tx.booked_date < spec.start:
        return False
    if spec.end is not None and tx.booked_date > spec.end:
        return False
    magnitude = abs(tx.amount)
    if spec.min_amount is not None and magnitude < spec.min_amount:
        return False
    if spec.max_amount is not None and magnitude > spec.max_amount:
        return False
    if spec.category_id is not None and tx.category_id != spec.category_id:
        return False
    if spec.account_id is not None and tx.account_id != spec.account_id:
        return False
    if spec.direction == "expense" and tx.amount >= 0:
        return False
    if spec.direction == "income" and tx.amount <= 0:
        return False

    return True


def _breakdown(
    session: Session, txns: list[Transaction], group_by: str | None
) -> list[CategoryTotal] | list[MonthTotals]:
    """Same bucketing shapes the overview uses — but over an arbitrary matched set,
    and by magnitude (direction is already filtered), so income-by-category works too."""
    if group_by == "category":
        names = {c.id: c.name for c in list_categories(session) if c.id is not None}
        totals: dict[int | None, int] = defaultdict(int)
        for tx in txns:
            totals[tx.category_id] += abs(tx.amount)
        rows = [
            CategoryTotal(
                category_id=cid,
                name=UNCATEGORIZED_LABEL if cid is None else names.get(cid, f"#{cid}"),
                total=total,
            )
            for cid, total in totals.items()
        ]
        rows.sort(key=lambda c: c.total, reverse=True)

        return rows

    if group_by == "month":
        spending: dict[str, int] = defaultdict(int)
        income: dict[str, int] = defaultdict(int)
        for tx in txns:
            key = tx.booked_date.strftime("%Y-%m")
            if tx.amount < 0:
                spending[key] -= tx.amount
            elif tx.amount > 0:
                income[key] += tx.amount
        months = sorted(set(spending) | set(income))

        return [MonthTotals(month=m, spending=spending[m], income=income[m]) for m in months]

    return []


def run_query(session: Session, spec: QuerySpec) -> QueryResult:
    """Filter :func:`spendable_transactions` (transfers/loans already excluded) by
    ``spec`` in pure Python, then total + bucket. ``interpretation`` is filled by
    :func:`answer`."""
    matches = [tx for tx in spendable_transactions(session) if _matches(tx, spec)]
    total = sum(abs(tx.amount) for tx in matches)
    rows = sorted(matches, key=lambda t: t.booked_date, reverse=True)[:_MAX_ROWS]

    return QueryResult(
        interpretation="",
        total=total,
        rows=rows,
        matched=len(matches),
        breakdown=_breakdown(session, matches, spec.group_by),
        group_by=spec.group_by,
        ok=True,
    )


def _failed(message: str) -> QueryResult:
    return QueryResult(
        interpretation=message,
        total=0,
        rows=[],
        matched=0,
        breakdown=[],
        group_by=None,
        ok=False,
    )


def answer(
    session: Session,
    question: str,
    *,
    settings: Settings | None = None,
    client: OllamaClient | None = None,
) -> QueryResult:
    """Orchestrate: gate on ``llm_enabled``, parse → validate → run. Never raises —
    a down piec or an uninterpretable question returns an ``ok=False`` result."""
    settings = settings or get_settings()
    if not settings.llm_enabled:
        return _failed("Natural-language queries are disabled.")

    categories = list_categories(session)
    accounts = list_accounts(session)
    client = client or OllamaClient.from_settings(settings)
    try:
        raw = client.parse_query(
            question,
            categories=[c.name for c in categories],
            accounts=[a.name for a in accounts],
            today=local_today(),
        )
    except OllamaError:
        return _failed(_COULDNT_INTERPRET)

    interpretation = raw.get("interpretation")
    if not isinstance(interpretation, str) or not interpretation.strip():
        return _failed(_COULDNT_INTERPRET)

    # A no-filter spec is a valid broad total — only a real parse failure is not-ok.
    result = run_query(session, build_spec(raw, categories, accounts))

    return replace(result, interpretation=interpretation.strip())
