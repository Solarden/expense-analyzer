"""Jinja2 setup for the dashboard (the working surface, design §8).

Server-rendered and deliberately plain — the pretty, glanceable layer lives in
Home Assistant, so this surface stays cheap to build and light on the Pi.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from expense_analyzer.money import format_pln

_TEMPLATE_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
templates.env.filters["pln"] = format_pln
