"""HTTP API routers.

Each functional area gets its own module with an ``APIRouter`` that is included
in the app in main.py. Keeps endpoint definitions out of the app factory as the
surface grows (expenses, transfers, loans, import, budgets, ...).
"""
