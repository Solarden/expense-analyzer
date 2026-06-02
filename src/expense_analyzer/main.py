from fastapi import FastAPI

# Importing the importers package registers the available bank parsers.
import expense_analyzer.importers.registry  # noqa: F401
from expense_analyzer import __version__
from expense_analyzer.api import dashboard, health
from expense_analyzer.config import get_settings
from expense_analyzer.logging_config import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.debug)

    app = FastAPI(title=settings.app_name, version=__version__)
    app.include_router(health.router)
    app.include_router(dashboard.router)
    return app


app = create_app()
