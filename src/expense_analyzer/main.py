from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# Importing the importers package registers the available bank parsers.
import expense_analyzer.importers.registry  # noqa: F401
from expense_analyzer import __version__, api
from expense_analyzer.auth import NotAuthenticatedError
from expense_analyzer.config import INSECURE_DEFAULT_SECRET, get_settings
from expense_analyzer.logging_config import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.debug)

    # Fail closed: never sign session cookies with the public default secret in
    # a real run. Compose already enforces EA_SECRET_KEY; this also covers manual
    # runs. Debug mode (e.g. `make dev`) is allowed to use the insecure default.
    if settings.secret_key == INSECURE_DEFAULT_SECRET and not settings.debug:
        raise RuntimeError(
            "EA_SECRET_KEY is not set (using the insecure default). Set it to a "
            'long random value, e.g. `python -c "import secrets; '
            'print(secrets.token_urlsafe(48))"`.'
        )

    app = FastAPI(title=settings.app_name, version=__version__)
    # Signed-cookie sessions. SameSite=Lax keeps the cookie off cross-site POSTs
    # (baseline CSRF protection); https_only stays off for plain-HTTP LAN use.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.secure_cookies,
    )

    # Vendored static assets (Chart.js for the overview charts) — served locally
    # so the Pi never reaches out to a CDN (design: stays fully offline).
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    # One router per domain, registered from a single list (see api/__init__.py).
    for router in api.routers:
        app.include_router(router)

    @app.exception_handler(NotAuthenticatedError)
    async def _redirect_to_login(request: Request, exc: NotAuthenticatedError) -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    return app


app = create_app()
