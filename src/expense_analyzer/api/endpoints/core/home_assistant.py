"""Home Assistant page (design §9): MQTT push status and a manual publish.

Shows whether the MQTT push is configured, the topic layout HA will see, and a
live preview of the metrics that would be published. The "Publish now" button
mirrors the Investments page's "Fetch now": it pushes immediately so you can
verify the HA wiring without waiting for the worker's interval.

Hidden/disabled unless MQTT is configured (``EA_MQTT_HOST``); with no host the
app opens no MQTT connection at all. Handlers stay thin — gathering lives in
:mod:`expense_analyzer.ha.metrics`, publishing in :mod:`expense_analyzer.ha.mqtt`
— and a failed publish becomes a red flash, never a 500.
"""

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from expense_analyzer.api.deps import CurrentUser, DbSession
from expense_analyzer.auth import require_user
from expense_analyzer.config import get_settings
from expense_analyzer.ha import discovery
from expense_analyzer.ha.metrics import collect_metrics
from expense_analyzer.ha.mqtt import MqttError, publish_snapshot
from expense_analyzer.models import Owner
from expense_analyzer.templating import templates

router = APIRouter(
    prefix="/dashboard/home-assistant",
    tags=["home-assistant"],
    dependencies=[Depends(require_user)],
)


def _context(session: Session, user: Owner, **extra) -> dict:
    settings = get_settings()
    base = settings.mqtt_base_topic
    return {
        "user": user,
        "mqtt_configured": settings.mqtt_configured,
        "mqtt_host": settings.mqtt_host,
        "mqtt_port": settings.mqtt_port,
        "mqtt_interval": settings.mqtt_publish_interval_minutes,
        "state_topic": discovery.state_topic(base),
        "discovery_prefix": settings.mqtt_discovery_prefix,
        "metrics": collect_metrics(session),
        **extra,
    }


@router.get("", response_class=HTMLResponse)
def home_assistant_page(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    return templates.TemplateResponse(request, "core/home_assistant.html", _context(session, user))


@router.post("/publish", response_class=HTMLResponse)
def publish_now(request: Request, user: CurrentUser, session: DbSession) -> HTMLResponse:
    settings = get_settings()
    error: str | None = None
    flash: str | None = None

    if not settings.mqtt_configured:
        error = "MQTT is not configured. Set EA_MQTT_HOST (and restart the app)."
    else:
        try:
            count = publish_snapshot(session, settings)
        except MqttError as exc:
            error = f"Could not publish to Home Assistant: {exc}"
        else:
            flash = f"Published {count} sensors to Home Assistant via MQTT."

    return templates.TemplateResponse(
        request,
        "core/home_assistant.html",
        _context(session, user, error=error, flash=flash),
        status_code=status.HTTP_200_OK if error is None else status.HTTP_400_BAD_REQUEST,
    )
