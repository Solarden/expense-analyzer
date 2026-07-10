"""HTTP layer.

Route modules live in :mod:`expense_analyzer.api.endpoints` — one router per
domain (the dashboard surface, plus auth and health). Shared HTML-form input
models live in :mod:`expense_analyzer.api.forms`.

``routers`` is the single registration point: ``create_app`` just iterates it, so
adding a domain is one line here rather than another ``include_router`` in the
app factory.
"""

from expense_analyzer.api.endpoints.categorize import categorization, rules
from expense_analyzer.api.endpoints.core import (
    auth,
    health,
    home_assistant,
    settings,
    updates,
    upload,
    users,
)
from expense_analyzer.api.endpoints.money import overview, query, transactions, transfers
from expense_analyzer.api.endpoints.planning import budgets, loans, plan, subscriptions
from expense_analyzer.api.endpoints.wealth import investments, net_worth

# Order is cosmetic (paths don't overlap); health/auth first, then dashboard domains.
routers = (
    health.router,
    auth.router,
    settings.router,
    overview.router,
    transactions.router,
    transfers.router,
    query.router,
    loans.router,
    budgets.router,
    plan.router,
    subscriptions.router,
    rules.router,
    categorization.router,
    investments.router,
    net_worth.router,
    home_assistant.router,
    upload.router,
    users.router,
    updates.router,
)
