"""logs_routes.py - /events (the app's log, newest first, filtered by level) and /logs
(the log files, to download), the way Sonarr and Radarr show System -> Events and Logs"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from web.logs import LEVELS, EventFilters, event_page, log_file, log_files
from web.routes import templates

router = APIRouter()


def parse_filters(request: Request) -> EventFilters:
    params = request.query_params
    level = params.get("level") or "info"
    if level not in LEVELS:
        raise HTTPException(status_code=422, detail=f"level must be one of {', '.join(LEVELS)}")
    try:
        page = int(params.get("page") or 1)
    except ValueError:
        raise HTTPException(status_code=422, detail="page must be a whole number") from None
    if not 1 <= page <= 100_000:
        raise HTTPException(status_code=422, detail="page must be 1 or more")
    return EventFilters(level, params.get("requests") in ("1", "true", "on"), page)


@router.get("/events", response_class=HTMLResponse)
def events_page(request: Request) -> HTMLResponse:
    page = event_page(parse_filters(request))
    return templates.TemplateResponse(
        request,
        "events.html",
        {"user": request.state.user, "page": page, "filters": page.filters, "LEVELS": LEVELS},
    )


@router.get("/api/events")
def api_events(request: Request) -> JSONResponse:
    page = event_page(parse_filters(request))
    return JSONResponse(
        {
            "events": [e.to_dict() for e in page.events],
            "total": page.total,
            "page": page.filters.page,
            "pages": page.pages,
        }
    )


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "logs.html", {"user": request.state.user, "files": log_files()}
    )


@router.get("/logs/files/{name}")
def download_log(name: str) -> PlainTextResponse:
    text = log_file(name)
    if text is None:
        raise HTTPException(status_code=404)
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="{name}"'})
