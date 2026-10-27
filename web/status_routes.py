"""status_routes.py - the status page: radarr, sonarr, sabnzbd, seerr and plex right now"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from config.models import ConfigSettings
from web.jobs import utc_now
from web.routes import templates
from web.secret_box import SecretError
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
