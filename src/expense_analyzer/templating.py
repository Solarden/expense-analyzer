"""Jinja2 setup for the dashboard (the working surface, design §8).

Server-rendered and deliberately plain — the pretty, glanceable layer lives in
Home Assistant, so this surface stays cheap to build and light on the Pi.
"""

from datetime import date
from pathlib import Path

from fastapi.templating import Jinja2Templates

from expense_analyzer.config import get_settings
from expense_analyzer.money import format_pln, format_quantity, from_minor_units

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def format_date(value: date) -> str:
    """Display a date in day-month-year order (Polish convention), e.g. 03.06.2026."""
    return value.strftime("%d.%m.%Y")


def format_plain_amount(minor: int) -> str:
    """A bare decimal magnitude for copy-paste into a bank transfer form, e.g.
    ``-300000`` -> ``"3000.00"`` (no grouping, no currency, no sign). The display
    ``pln`` filter is for reading; this one is for pasting."""
    return str(from_minor_units(abs(minor)))


templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
templates.env.filters["pln"] = format_pln
templates.env.filters["qty"] = format_quantity
templates.env.filters["dpl"] = format_date
templates.env.filters["plain"] = format_plain_amount
# Available to every template (e.g. the AGPL §13 "Source" link in base.html)
# without threading it through each handler's context.
templates.env.globals["source_url"] = get_settings().source_url
