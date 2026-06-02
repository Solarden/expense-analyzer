"""Data-access layer: database queries grouped by domain.

Route handlers stay thin and free of query logic. As the schema grows
(reconciliation, transfers, budgets, subscriptions, ...) the queries live here —
one module per domain — instead of sprawling across the API layer.

Deliberately plain: functions that take a ``Session`` and return models. No
repository pattern, unit-of-work, or ORM abstraction — that would be the
over-engineering the design warns against for a solo household app.
"""
