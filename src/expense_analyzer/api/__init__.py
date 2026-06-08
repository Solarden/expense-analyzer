"""HTTP layer.

Route modules live in :mod:`expense_analyzer.api.endpoints` — one router per
domain (the dashboard surface, plus auth and health). Shared HTML-form input
models live in :mod:`expense_analyzer.api.forms`.

``routers`` is the single registration point: ``create_app`` just iterates it, so
adding a domain is one line here rather than another ``include_router`` in the
app factory.
"""

from expense_analyzer.api.endpoints import (
    auth,
    budgets,
    categorization,
    health,
    home,
    home_assistant,
    investments,
    loans,
    net_worth,
    overview,
    rules,
    subscriptions,
    transactions,
    transfers,
    upload,
    users,
)

# Order is cosmetic (paths don't overlap); health/auth first, then dashboard domains.
routers = (
    health.router,
    auth.router,
    home.router,
    overview.router,
    transactions.router,
    transfers.router,
    loans.router,
    budgets.router,
    subscriptions.router,
    rules.router,
    categorization.router,
    investments.router,
    net_worth.router,
    home_assistant.router,
    upload.router,
    users.router,
)
