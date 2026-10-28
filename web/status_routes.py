"""status_routes.py - the status page: radarr, sonarr, sabnzbd, seerr and plex right now,
the history of problems web.health recorded, and removing stuck downloads from the queues"""

import logging
from dataclasses import asdict
from datetime import timedelta
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from config.models import ConfigSettings
from web.health import (
    CHECK_INTERVAL,
    KEEP,
    LABELS,
    STATUS_SETTING,
    incident_dict,
    incidents,
    item_key,
    problem_key,
    span,
    summary,
)
from web.jobs import utc_now
from web.routes import form_lists, templates
from web.secret_box import SecretError
from web.stats import sync_status
from web.status import (
    BLOCKLIST,
    MAX_PROBLEMS,
    REMOVAL_METHODS,
    ArrQueue,
    ServiceStatus,
    StuckDownload,
    check_all,
    clear_stuck,
    failure,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# the page refreshes itself this often
REFRESH_SECONDS = 60
# what an unconnected card says it needs
NEEDS = {
    "plex": ("Tautulli", "tautulli"),
    "radarr": ("Radarr", "radarr"),
    "sonarr": ("Sonarr", "sonarr"),
    "bazarr": ("Bazarr", "bazarr"),
    "sabnzbd": ("SABnzbd", "sabnzbd"),
    "seerr": ("Seerr", "seerr"),
}
# the arrs whose stuck downloads can be removed here
QUEUES = {"radarr": "Radarr", "sonarr": "Sonarr"}
# the status page's recent problems
RECENT = timedelta(days=7)
RECENT_LIMIT = 10
# the history page's periods, in days
HISTORY_DAYS = {1: "Last 24 hours", 7: "Last 7 days", 30: "Last 30 days", 90: "Last 90 days", 365: "Last 12 months"}
DEFAULT_HISTORY_DAYS = 30


def statuses(
    request: Request,
) -> tuple[list[ServiceStatus], str | None, dict[str, dict[str, str]], list[dict[str, str]]]:
    """every service's status, links to them and to other tools, or none and why the
    settings can't load"""
    try:
        settings: ConfigSettings = request.app.state.load_config()
    except (SecretError, ValueError) as e:
        return [], str(e), {}, []
    others = [{"name": l.name, "local": l.url, "public": l.public_url, "service": l.service} for l in settings.links]
    return check_all(settings, utc_now()), None, links(settings), others


def links(settings: ConfigSettings) -> dict[str, dict[str, str]]:
    """each service's own web ui, {"local", "public"} (plex's is tautulli's)"""
    found = {}
    for service, config in (
        ("plex", settings.tautulli),
        ("radarr", settings.radarr),
        ("sonarr", settings.sonarr),
        ("bazarr", settings.bazarr),
        ("sabnzbd", settings.sabnzbd),
        ("seerr", settings.seerr),
    ):
        if config is None:
            continue
        local = getattr(config, "baseurl", None) or getattr(config, "url", None)
        urls = {"local": local, "public": config.public_url}
        if urls := {k: v for k, v in urls.items() if v}:
            found[service] = urls
    return found


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request) -> HTMLResponse:
    checked_at = utc_now()
    services, config_error, service_links, other_links = statuses(request)
    recent = incidents(request.app.state.engine, checked_at - RECENT)
    # ongoing first, so one that started days ago isn't cut off by newer ones that cleared
    recent.sort(key=lambda i: i.ended_at is not None)
    ongoing = [i for i in recent if i.ended_at is None]
    # how long each open problem has lasted, by (service, incident key), and each
    # service's oldest one (incidents are newest first)
    open_for = {(i.service, i.key): span(checked_at - i.started_at) for i in ongoing}
    open_since: dict[str, str] = {}
    for i in reversed(ongoing):
        open_since.setdefault(i.service, span(checked_at - i.started_at))
    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "user": request.state.user,
            "services": services,
            "config_error": config_error,
            "links": service_links,
            "other_links": other_links,
            "checked_at": checked_at,
            # ?refresh=0 pauses it, for reading (or a screen reader) without the page reloading
            "refresh": 0 if request.query_params.get("refresh") == "0" else REFRESH_SECONDS,
            "needs": NEEDS,
            "max_problems": MAX_PROBLEMS,
            "queues": QUEUES,
            "recent": [incident_dict(i, checked_at) for i in recent[:RECENT_LIMIT]],
            "recent_total": len(recent),
            "open_for": open_for,
            "open_since": open_since,
            "problem_key": problem_key,
            "item_key": item_key,
            "health": sync_status(request.app.state.engine, STATUS_SETTING),
            "check_minutes": int(CHECK_INTERVAL.total_seconds() // 60),
        },
    )


@router.get("/api/status")
def api_status(request: Request) -> JSONResponse:
    checked_at = utc_now()
    services, config_error, _, _ = statuses(request)
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


# ---------------------------------------------------------------------------
# stuck downloads
# ---------------------------------------------------------------------------


class QueueError(Exception):
    """why a queue can't be read or changed, shown as is"""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def arr_queue(request: Request, service: str) -> ArrQueue:
    if service not in QUEUES:
        raise HTTPException(status_code=404)
    try:
        settings: ConfigSettings = request.app.state.load_config()
    except (SecretError, ValueError) as e:
        raise QueueError(f"The settings can't be loaded: {e}", 503)
    arr = getattr(settings, service)
    if arr is None or not arr.baseurl or not arr.apikey:
        raise QueueError(f"{QUEUES[service]} isn't connected.", 409)
    return ArrQueue(service, QUEUES[service], arr)


def read_stuck(queue: ArrQueue) -> list[StuckDownload]:
    try:
        return queue.stuck()
    except Exception as e:
        raise QueueError(failure(e, f"Reading {queue.label}'s queue", queue.apikey), 502)


def remove_stuck(
    request: Request, queue: ArrQueue, keys: list[str], method: str, blocklist: str
) -> tuple[list[StuckDownload], int]:
    """removes the selected stuck downloads and logs who did"""
    try:
        removed, gone = clear_stuck(queue, keys, method, blocklist)
    except Exception as e:
        raise QueueError(failure(e, f"Removing from {queue.label}'s queue", queue.apikey), 502)
    if removed:
        logger.info(
            "WEB\t %s removed %s stuck download(s) from %s's queue (%s, %s): %s",
            request.state.user,
            len(removed),
            queue.label,
            method,
            blocklist,
            "; ".join(d.release or ", ".join(d.titles) for d in removed),
        )
        # the health checks see they're gone without waiting 5 minutes
        request.app.state.health.sync_now()
    return removed, gone


def queue_page(request: Request, service: str, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    downloads: list[StuckDownload] = []
    if error is None:
        try:
            downloads = read_stuck(arr_queue(request, service))
        except QueueError as e:
            error = str(e)
    params = request.query_params
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "user": request.state.user,
            "service": service,
            "label": QUEUES[service],
            "downloads": downloads,
            "error": error,
            "removed": int(params["removed"]) if params.get("removed", "").isdigit() else None,
            "gone": int(params["gone"]) if params.get("gone", "").isdigit() else 0,
            "methods": REMOVAL_METHODS,
            "blocklist": BLOCKLIST,
        },
        status_code=status_code,
    )


@router.get("/status/{service}/queue", response_class=HTMLResponse)
def queue_get(request: Request, service: str) -> HTMLResponse:
    if service not in QUEUES:
        raise HTTPException(status_code=404)
    return queue_page(request, service)


@router.post("/status/{service}/queue/remove", response_model=None)
async def queue_remove(request: Request, service: str) -> HTMLResponse | RedirectResponse:
    if service not in QUEUES:
        raise HTTPException(status_code=404)
    fields = await form_lists(request)
    keys = fields.get("download", [])
    method = (fields.get("method") or ["client"])[-1]
    blocklist = (fields.get("blocklist") or ["none"])[-1]
    if method not in REMOVAL_METHODS or blocklist not in BLOCKLIST:
        raise HTTPException(status_code=422)
    if not keys:
        return queue_page(request, service, "Select at least one download.", 422)
    try:
        queue = arr_queue(request, service)
        removed, gone = remove_stuck(request, queue, keys, method, blocklist)
    except QueueError as e:
        return queue_page(request, service, str(e), e.status_code)
    query = urlencode({"removed": len(removed), "gone": gone})
    return RedirectResponse(f"/status/{service}/queue?{query}", status_code=303)


class RemoveRequest(BaseModel):
    # StuckDownload keys from GET /api/status/{service}/queue
    downloads: list[str]
    method: Literal["client", "ignore"] = "client"
    blocklist: Literal["none", "search", "only"] = "none"


@router.get("/api/status/{service}/queue")
def api_queue(request: Request, service: str) -> JSONResponse:
    try:
        downloads = read_stuck(arr_queue(request, service))
    except QueueError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status_code)
    return JSONResponse({"downloads": [asdict(d) for d in downloads]})


@router.post("/api/status/{service}/queue/remove")
def api_queue_remove(request: Request, service: str, body: RemoveRequest) -> JSONResponse:
    if not body.downloads:
        return JSONResponse({"error": "Select at least one download."}, status_code=422)
    try:
        queue = arr_queue(request, service)
        removed, gone = remove_stuck(request, queue, body.downloads, body.method, body.blocklist)
    except QueueError as e:
        return JSONResponse({"error": str(e)}, status_code=e.status_code)
    return JSONResponse({"removed": [asdict(d) for d in removed], "gone": gone})
