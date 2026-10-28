"""playback_routes.py - the playback page: how plex streamed recent plays and why it
transcoded, from the table web.playback syncs"""

from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from web.jobs import utc_now
from web.playback import (
    BACKFILL,
    CAUSES,
    DECISIONS,
    DEFAULT_WINDOW,
    FILE_MIN_USERS,
    INTERVAL,
    KEEP,
    STATUS_SETTING,
    WINDOWS,
    PlaybackSync,
    report,
    window_plays,
)
from web.routes import missing_connections, templates
from web.stats import sync_status

router = APIRouter()


@dataclass
class Filters:
    window: str = DEFAULT_WINDOW
    user: int | None = None

    def query(self, **changes: Any) -> dict[str, Any]:
        """these filters as query params, with changes"""
        values = {**self.__dict__, **changes}
        return {k: v for k, v in values.items() if v not in (None, "")}

    @property
    def filtered(self) -> bool:
        return self.query() != {"window": DEFAULT_WINDOW}


def parse_filters(request: Request) -> Filters:
    params = request.query_params
    filters = Filters()
    if params.get("window"):
        if params["window"] not in WINDOWS:
            raise HTTPException(status_code=422, detail=f"window must be one of {', '.join(WINDOWS)}")
        filters.window = params["window"]
    if params.get("user"):
        try:
            filters.user = int(params["user"])
        except ValueError:
            raise HTTPException(status_code=422, detail="user must be a whole number") from None
    return filters


def playback_sync(request: Request) -> PlaybackSync:
    return request.app.state.playback


@router.get("/playback", response_class=HTMLResponse)
def playback_page(request: Request) -> HTMLResponse:
    filters = parse_filters(request)
    plays = window_plays(request.app.state.engine, utc_now() - WINDOWS[filters.window][1])
    # everyone in the window, so the user select keeps them while one is picked
    users = sorted({p.user_id: p.user for p in plays}.items(), key=lambda u: u[1].casefold())
    if filters.user is not None:
        plays = [p for p in plays if p.user_id == filters.user]
    return templates.TemplateResponse(
        request,
        "playback.html",
        {
            "user": request.state.user,
            "status": sync_status(request.app.state.engine, STATUS_SETTING),
            "syncing": playback_sync(request).running,
            "missing": missing_connections(request, "tautulli"),
            "filters": filters,
            "users": users,
            "report": report(plays),
            "keep_days": KEEP.days,
            "backfill_days": BACKFILL.days,
            "file_min_users": FILE_MIN_USERS,
            "interval_minutes": int(INTERVAL.total_seconds() // 60),
            "WINDOWS": WINDOWS,
            "DECISIONS": DECISIONS,
            "CAUSES": CAUSES,
        },
    )


@router.post("/playback/sync")
def sync_now(request: Request) -> RedirectResponse:
    playback_sync(request).sync_now()
    return RedirectResponse("/playback", status_code=303)


@router.get("/api/playback")
def api_playback(request: Request) -> JSONResponse:
    filters = parse_filters(request)
    since = utc_now() - WINDOWS[filters.window][1]
    status = sync_status(request.app.state.engine, STATUS_SETTING)
    return JSONResponse(
        jsonable_encoder(
            {
                "window": filters.window,
                "from": since,
                "synced_at": status.finished_at,
                "sync_error": status.error,
                **report(window_plays(request.app.state.engine, since, filters.user)),
            }
        )
    )
