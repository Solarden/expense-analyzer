from fastapi import APIRouter, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlmodel import Session

from expense_analyzer.db import get_engine

router = APIRouter(tags=["meta"])


@router.get("/")
def root() -> RedirectResponse:
    # The bare domain is for humans → send them straight to the dashboard (which
    # bounces to /login if they're not signed in). Liveness probes use /health.
    # Temporary redirect so browsers never cache the bare domain as permanent.
    return RedirectResponse("/dashboard", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@router.get("/health")
def health() -> JSONResponse:
    """Liveness + DB reachability check (also used by the Docker healthcheck).

    503 when the database is unreachable — the DB is an external server that
    can go down independently of the app, and a finance app without its data
    is not healthy. Docker only *marks* the container unhealthy (no restart
    loop), and the deploy's health wait correctly refuses to declare success.
    """
    db_ok = True
    try:
        with Session(get_engine()) as session:
            session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    return JSONResponse(
        status_code=status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "status": "ok" if db_ok else "degraded",
            "database": "ok" if db_ok else "unreachable",
            "dialect": get_engine().dialect.name,
        },
    )
