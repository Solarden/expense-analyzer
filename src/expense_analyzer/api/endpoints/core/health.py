from fastapi import APIRouter
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
def health() -> dict[str, str]:
    """Liveness + DB reachability check (also used by the Docker healthcheck)."""
    db_ok = True
    journal_mode = None
    try:
        with Session(get_engine()) as session:
            journal_mode = session.execute(text("PRAGMA journal_mode")).scalar()
    except Exception:
        db_ok = False

    return {
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "unreachable",
        "journal_mode": journal_mode or "unknown",
    }
