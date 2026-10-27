"""status_routes.py - the status page: radarr, sonarr, sabnzbd, seerr and plex right now,
and the history of problems web.health recorded"""

from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from config.models import ConfigSettings
from web.health import CHECK_INTERVAL, KEEP, LABELS, STATUS_SETTING, incident_dict, incidents, summary
from web.jobs import utc_now
from web.routes import templates
from web.secret_box import SecretError
from web.stats import sync_status
from web.status import MAX_PROBLEMS, ServiceStatus, check_all

router = APIRouter()

# the page refreshes itself this often
REFRESH_SECONDS = 60
# what an unconnected card says it needs
NEEDS = {
    "plex": ("Tautulli", "tautulli"),
    "radarr": ("Radarr", "radarr"),
    "sonarr": ("Sonarr", "sonarr"),
    "sabnzbd": ("SABnzbd", "sabnzbd"),
    "seerr": ("Seerr", "seerr"),
}
# the status page's recent problems
RECENT = timedelta(days=7)
RECENT_LIMIT = 10
# the history page's periods, in days
HISTORY_DAYS = {1: "Last 24 hours", 7: "Last 7 days", 30: "Last 30 days", 90: "Last 90 days", 365: "Last 12 months"}
DEFAULT_HISTORY_DAYS = 30


def statuses(request: Request) -> tuple[list[ServiceStatus], str | None]:
    """every service's status, or none and why the settings can't load"""
    try:
        settings: ConfigSettings = request.app.state.load_config()
    except (SecretError, ValueError) as e:
        return [], str(e)
    return check_all(settings, utc_now()), None


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request) -> HTMLResponse:
    checked_at = utc_now()
    services, config_error = statuses(request)
    recent = incidents(request.app.state.engine, checked_at - RECENT)
    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "user": request.state.user,
            "services": services,
            "config_error": config_error,
            "checked_at": checked_at,
            "refresh": REFRESH_SECONDS,
            "needs": NEEDS,
            "max_problems": MAX_PROBLEMS,
            "recent": [incident_dict(i, checked_at) for i in recent[:RECENT_LIMIT]],
            "recent_total": len(recent),
            "health": sync_status(request.app.state.engine, STATUS_SETTING),
            "check_minutes": int(CHECK_INTERVAL.total_seconds() // 60),
        },
    )


@router.get("/api/status")
def api_status(request: Request) -> JSONResponse:
    checked_at = utc_now()
    services, config_error = statuses(request)
    if config_error:
        return JSONResponse({"error": config_error}, status_code=503)
    return JSONResponse(
        {
            "checked_at": checked_at.isoformat(),
            "services": [s.to_dict() for s in services],
        }
    )


def history_filters(request: Request) -> tuple[int, str | None]:
    params = request.query_params
    service = params.get("service") or None
    if service is not None and service not in LABELS:
        raise HTTPException(status_code=422, detail=f"service must be one of {', '.join(LABELS)}")
    try:
        days = int(params.get("days") or DEFAULT_HISTORY_DAYS)
    except ValueError:
        days = 0
    if days not in HISTORY_DAYS:
        raise HTTPException(status_code=422, detail=f"days must be one of {', '.join(map(str, HISTORY_DAYS))}")
    return days, service


@router.get("/status/history", response_class=HTMLResponse)
def history_page(request: Request) -> HTMLResponse:
    days, service = history_filters(request)
    now = utc_now()
    since = now - timedelta(days=days)
    rows = incidents(request.app.state.engine, since, service)
    return templates.TemplateResponse(
        request,
        "status_history.html",
        {
            "user": request.state.user,
            "days": days,
            "service": service,
            "incidents": [incident_dict(i, now) for i in rows],
            "summary": summary(rows, since, now),
            "health": sync_status(request.app.state.engine, STATUS_SETTING),
            "check_minutes": int(CHECK_INTERVAL.total_seconds() // 60),
            "keep_days": KEEP.days,
            "LABELS": LABELS,
            "HISTORY_DAYS": HISTORY_DAYS,
            "DEFAULT_HISTORY_DAYS": DEFAULT_HISTORY_DAYS,
        },
    )


@router.get("/api/status/history")
def api_history(request: Request) -> JSONResponse:
    days, service = history_filters(request)
    now = utc_now()
    since = now - timedelta(days=days)
    rows = incidents(request.app.state.engine, since, service)
    health = sync_status(request.app.state.engine, STATUS_SETTING)
    return JSONResponse(
        jsonable_encoder(
            {
                "from": since,
                "checked_at": health.finished_at,
                "check_error": health.error,
                "summary": summary(rows, since, now),
                "incidents": [incident_dict(i, now) for i in rows],
            }
        )
    )
