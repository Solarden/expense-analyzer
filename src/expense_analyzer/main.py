from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

# Importing the importers package registers the available bank parsers.
import expense_analyzer.importers.registry  # noqa: F401
from expense_analyzer import __version__, api
from expense_analyzer.auth import NotAuthenticatedError, NotAuthorizedError
from expense_analyzer.config import INSECURE_DEFAULT_SECRET, get_settings
from expense_analyzer.logging_config import configure_logging
from expense_analyzer.templating import templates

# Set here rather than in the Caddyfile because not every deployment is served through the
# proxy this repo ships — one behind its own would otherwise get none of this.
SECURITY_HEADERS = {
    # 'unsafe-inline' is required, not lazy: the chart pages inline their data in a
    # <script> block and nearly every template carries inline style= attributes.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
    # The app is all same-origin HTML forms, so framing is never legitimate — and
    # SameSite=Lax does not help inside a frame, where a POST is same-site.
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    # Browsers ignore this over plain http, so it costs a LAN install nothing.
    "Strict-Transport-Security": "max-age=31536000",
}


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

    # FastAPI registers /docs, /redoc and /openapi.json itself, outside the routers
    # that carry require_user — so on a shared LAN they hand any device the full
    # route map pre-login. Keep them for local debugging only.
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
        openapi_url="/openapi.json" if settings.debug else None,
    )
    # Signed-cookie sessions. SameSite=Lax keeps the cookie off cross-site POSTs
    # (baseline CSRF protection); https_only stays off for plain-HTTP LAN use.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.secure_cookies,
    )

    # Vendored static assets (Chart.js for the overview charts) — served locally
    # so the host never reaches out to a CDN (design: stays fully offline).
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    # Registered after the session middleware, so it wraps it and covers error responses
    # and redirects too, not just the routes below.
    @app.middleware("http")
    async def _add_security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)

        return response

    # One router per domain, registered from a single list (see api/__init__.py).
    for router in api.routers:
        app.include_router(router)

    @app.exception_handler(NotAuthenticatedError)
    async def _redirect_to_login(request: Request, exc: NotAuthenticatedError) -> RedirectResponse:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.exception_handler(NotAuthorizedError)
    async def _forbidden(request: Request, exc: NotAuthorizedError) -> HTMLResponse:
        # The user is logged in but lacks the admin role; show a 403, don't redirect.
        return templates.TemplateResponse(
            request, "core/forbidden.html", {}, status_code=status.HTTP_403_FORBIDDEN
        )

    return app


app = create_app()
