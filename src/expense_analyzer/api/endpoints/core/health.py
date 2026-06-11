from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlmodel import Session

from expense_analyzer import __version__
from expense_analyzer.config import get_settings
from expense_analyzer.db import get_engine

router = APIRouter(tags=["meta"])


@router.get("/")
def root() -> dict[str, str]:
    settings = get_settings()

    return {"app": settings.app_name, "version": __version__}


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
