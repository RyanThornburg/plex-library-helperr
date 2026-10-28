"""stats_routes.py - the requests page: seerr requests and whether the requester watched
them, from the tables web.stats syncs"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from web.db import MediaRequest
from web.jobs import utc_now
from web.routes import missing_connections, templates
from web.stats import StatsSync, sync_status

router = APIRouter()

# a request's progress, for the requester
PROGRESS = {
    "not_started": "Not started",
    "started": "Started",
    "finished": "Finished",
    "unavailable": "Not in Plex",
    "unmatched": "No Plex user",
}
TYPES = {"movie": "Movie", "tv": "TV"}
# when the requests were made. Years start on 1 January utc, like every stored time
PERIODS = {
    "year": "This year",
    "last_year": "Last year",
    "30d": "Last 30 days",
    "90d": "Last 90 days",
    "365d": "Last 12 months",
    "all": "All time",
    "custom": "Custom",
}
DEFAULT_PERIOD = "year"
# "requests made ..."
PERIOD_PHRASES = {
    "year": "this year",
    "last_year": "last year",
    "30d": "in the last 30 days",
    "90d": "in the last 90 days",
    "365d": "in the last 12 months",
    "all": "at any time",
}
# the users table's "not started" column counts requests available this long
DEFAULT_STALE_DAYS = 30


def progress(r: MediaRequest) -> str:
    if r.items == 0:
        return "unavailable"
    if r.requester_user_id is None:
        return "unmatched"
    if r.watched >= r.items:
        return "finished"
    # playing episodes plex no longer has counts too
    return "started" if r.started or r.requester_plays else "not_started"


def days_available(r: MediaRequest, now: datetime) -> int | None:
    return (now - r.available_at).days if r.available_at else None


def request_dict(r: MediaRequest, now: datetime) -> dict[str, Any]:
    return {
        "id": r.id,
        "type": r.media_type,
        "title": r.title,
        "year": r.year,
        "tmdb_id": r.tmdb_id,
        "tvdb_id": r.tvdb_id,
        "status": r.status,
        "media_status": r.media_status,
        "is_4k": r.is_4k,
        "seasons": r.seasons,
        "requested_at": r.requested_at.isoformat(),
        "available_at": r.available_at.isoformat() if r.available_at else None,
        "days_available": days_available(r, now),
        "requester": {"id": r.requester_id, "name": r.requester, "plex_user_id": r.requester_user_id},
        "progress": progress(r),
        "items": r.items,
        "watched": r.watched,
        "started": r.started,
        "requester_plays": r.requester_plays,
        "last_played_at": r.last_played_at.isoformat() if r.last_played_at else None,
        "other_viewers": r.other_viewers,
    }


def period_range(period: str, since: date | None, until: date | None, now: datetime) -> tuple[datetime | None, datetime | None]:
    """the requested_at range of a period: start inclusive, end exclusive, None unbounded"""
    year = datetime(now.year, 1, 1)
    match period:
        case "year":
            return year, None
        case "last_year":
            return year.replace(year=now.year - 1), year
        case "30d" | "90d" | "365d":
            return now - timedelta(days=int(period[:-1])), None
        case "custom":
            return (
                datetime.combine(since, datetime.min.time()) if since else None,
                datetime.combine(until, datetime.min.time()) + timedelta(days=1) if until else None,
            )
    return None, None


@dataclass
class Filters:
    user: int | None = None
    type: str | None = None
    progress: str | None = None
    # available at least this many days
    min_days: int | None = None
    period: str = DEFAULT_PERIOD
    # only with period "custom", until is inclusive
    since: date | None = None
    until: date | None = None

    def query(self, **changes: Any) -> dict[str, Any]:
        """these filters as query params, with changes"""
        values = {**self.__dict__, **changes}
        if values["period"] != "custom":
            values["since"] = values["until"] = None
        return {k: v for k, v in values.items() if v not in (None, "")}

    @property
    def filtered(self) -> bool:
        """anything but the defaults"""
        return self.query() != {"period": DEFAULT_PERIOD}

    def period_label(self) -> str:
        """completes "requests made ..." """
        if self.period != "custom":
            return PERIOD_PHRASES[self.period]
        if self.since and self.until:
            return f"from {self.since} to {self.until}"
        if self.since:
            return f"since {self.since}"
        return f"up to {self.until}" if self.until else PERIOD_PHRASES["all"]

    def in_scope(self, r: MediaRequest, now: datetime) -> bool:
        """the filters the users table uses: period, type and requester"""
        start, end = period_range(self.period, self.since, self.until, now)
        if start is not None and r.requested_at < start:
            return False
        if end is not None and r.requested_at >= end:
            return False
        if self.user is not None and r.requester_id != self.user:
            return False
        if self.type and r.media_type != self.type:
            return False
        return True

    def matches(self, r: MediaRequest, now: datetime) -> bool:
        if not self.in_scope(r, now):
            return False
        if self.progress and progress(r) != self.progress:
            return False
        if self.min_days is not None:
            days = days_available(r, now)
            if days is None or days < self.min_days:
                return False
        return True


def parse_filters(request: Request) -> Filters:
    params = request.query_params
    filters = Filters()
    try:
        if params.get("user"):
            filters.user = int(params["user"])
        if params.get("min_days"):
            filters.min_days = int(params["min_days"])
            if filters.min_days < 0:
                raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="user and min_days must be whole numbers") from None
    if params.get("type"):
        if params["type"] not in TYPES:
            raise HTTPException(status_code=422, detail=f"type must be one of {', '.join(TYPES)}")
        filters.type = params["type"]
    if params.get("progress"):
        if params["progress"] not in PROGRESS:
            raise HTTPException(status_code=422, detail=f"progress must be one of {', '.join(PROGRESS)}")
        filters.progress = params["progress"]
    if params.get("period"):
        if params["period"] not in PERIODS:
            raise HTTPException(status_code=422, detail=f"period must be one of {', '.join(PERIODS)}")
        filters.period = params["period"]
    elif params.get("since") or params.get("until"):
        filters.period = "custom"
    if filters.period == "custom":
        try:
            if params.get("since"):
                filters.since = date.fromisoformat(params["since"])
            if params.get("until"):
                filters.until = date.fromisoformat(params["until"])
        except ValueError:
            raise HTTPException(status_code=422, detail="since and until must be dates (YYYY-MM-DD)") from None
        if filters.since and filters.until and filters.since > filters.until:
            raise HTTPException(status_code=422, detail="since must be on or before until")
    return filters


def all_requests(request: Request) -> list[MediaRequest]:
    with Session(request.app.state.engine) as session:
        return list(
            session.scalars(select(MediaRequest).order_by(MediaRequest.requested_at.desc()))
        )


def user_summaries(requests: list[MediaRequest], now: datetime, stale_days: int) -> list[dict[str, Any]]:
    """per requester: their requests (declined ones left out) and what became of them"""
    users: dict[int, dict[str, Any]] = {}
    counts: dict[int, defaultdict[str, int]] = {}
    for r in requests:
        if r.status == "declined":
            continue
        user = users.setdefault(
            r.requester_id,
            {"id": r.requester_id, "name": r.requester, "plex_user_id": r.requester_user_id, "last_requested_at": r.requested_at},
        )
        user["last_requested_at"] = max(user["last_requested_at"], r.requested_at)
        c = counts.setdefault(r.requester_id, defaultdict(int))
        c["requests"] += 1
        state = progress(r)
        c[state] += 1
        if state == "not_started" and (days_available(r, now) or 0) >= stale_days:
            c["stale"] += 1
    summaries = []
    for user_id, user in users.items():
        c = counts[user_id]
        available = c["requests"] - c["unavailable"]
        summaries.append(
            {
                **user,
                "last_requested_at": user["last_requested_at"].isoformat(),
                "requests": c["requests"],
                "available": available,
                "finished": c["finished"],
                "started": c["started"],
                "not_started": c["not_started"],
                "stale": c["stale"],
                # of what they could have watched
                "finished_percent": round(100 * c["finished"] / available) if available and user["plex_user_id"] is not None else None,
            }
        )
    return sorted(summaries, key=lambda u: (-u["requests"], u["name"].casefold()))


def stats_sync(request: Request) -> StatsSync:
    return request.app.state.stats


REQUESTS_SHOWN = 50


@router.get("/requests", response_class=HTMLResponse)
def requests_page(request: Request) -> HTMLResponse:
    filters = parse_filters(request)
    now = utc_now()
    requests = all_requests(request)
    shown = [r for r in requests if filters.matches(r, now)]
    stale_days = filters.min_days if filters.min_days is not None else DEFAULT_STALE_DAYS
    # everyone who ever asked, so a requester filter outside the period still shows
    requesters = sorted({r.requester_id: r.requester for r in requests}.items(), key=lambda u: u[1].casefold())
    return templates.TemplateResponse(
        request,
        "requests.html",
        {
            "user": request.state.user,
            "status": sync_status(request.app.state.engine),
            "syncing": stats_sync(request).running,
            "filters": filters,
            "requests": shown,
            # a year of requests is a long page, ?all=1 shows them all
            "limit": None if request.query_params.get("all") == "1" else REQUESTS_SHOWN,
            "missing": missing_connections(request, "seerr", "tautulli"),
            "total": len(requests),
            "users": user_summaries([r for r in requests if filters.in_scope(r, now)], now, stale_days),
            "requesters": requesters,
            "stale_days": stale_days,
            "now": now,
            "progress": progress,
            "days_available": days_available,
            "PROGRESS": PROGRESS,
            "TYPES": TYPES,
            "PERIODS": PERIODS,
        },
    )


@router.post("/requests/sync")
def sync_now(request: Request) -> RedirectResponse:
    stats_sync(request).sync_now()
    return RedirectResponse("/requests?synced=1", status_code=303)


@router.get("/api/requests")
def api_requests(request: Request) -> JSONResponse:
    filters = parse_filters(request)
    now = utc_now()
    status = sync_status(request.app.state.engine)
    return JSONResponse(
        {
            "synced_at": status.finished_at.isoformat() if status.finished_at else None,
            "sync_error": status.error,
            "requests": [request_dict(r, now) for r in all_requests(request) if filters.matches(r, now)],
        }
    )


@router.get("/api/requests/users")
def api_request_users(request: Request) -> JSONResponse:
    filters = parse_filters(request)
    now = utc_now()
    stale_days = filters.min_days if filters.min_days is not None else DEFAULT_STALE_DAYS
    start, end = period_range(filters.period, filters.since, filters.until, now)
    return JSONResponse(
        {
            "period": filters.period,
            "from": start.isoformat() if start else None,
            "to": end.isoformat() if end else None,
            "stale_days": stale_days,
            "users": user_summaries([r for r in all_requests(request) if filters.in_scope(r, now)], now, stale_days),
        }
    )
