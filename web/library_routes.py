"""library_routes.py - the library page: every movie, show and season in Plex with
everyone's plays of it, to find what nobody watches. From the tables web.stats syncs;
requesters come from the synced Seerr requests"""

import csv
import io
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from web.db import LibraryItem, MediaRequest
from web.jobs import utc_now
from web.routes import missing_connections, templates
from web.stats import LIBRARY_STATUS, StatsSync
from web.sync import sync_status

router = APIRouter()

# which items, by their plays and the days filter
WATCH = {
    "unwatched": "Never watched or stale",
    "never": "Never watched",
    "stale": "Watched, but not lately",
    "recent": "Watched lately",
    "all": "Everything",
}
DEFAULT_WATCH = "unwatched"
DEFAULT_DAYS = 180
TYPES = {"movie": "Movies", "show": "TV"}
# each sorts its natural way (oldest, fewest plays...), reverse flips it
SORTS = {
    "idle": "Longest idle",
    "title": "Title",
    "added": "Added",
    "last_played": "Last played",
    "plays": "Plays",
    "requester": "Requester",
}
DEFAULT_SORT = "idle"
# the requester filter's other values: requested by anyone, or by no one
ANY_REQUESTER = "any"
NO_REQUESTER = "none"
SHOWN = 100


@dataclass
class Filters:
    library: str | None = None
    type: str | None = None
    # tv as a row per season
    seasons: bool = False
    watch: str = DEFAULT_WATCH
    days: int = DEFAULT_DAYS
    # a seerr requester id, ANY_REQUESTER or NO_REQUESTER; None is either
    requester: str | None = None
    sort: str = DEFAULT_SORT
    reverse: bool = False

    def query(self, **changes: Any) -> dict[str, Any]:
        """these filters as query params, with changes, defaults left out"""
        values = {**self.__dict__, **changes}
        defaults = Filters().__dict__
        return {
            k: (1 if v is True else v)
            for k, v in values.items()
            if v not in (None, "", False) and v != defaults[k]
        }

    @property
    def filtered(self) -> bool:
        return bool(self.query())

    def kinds(self) -> set[str]:
        tv = "season" if self.seasons else "show"
        return {"movie"} if self.type == "movie" else {tv} if self.type == "show" else {"movie", tv}

    def in_scope(self, item: LibraryItem) -> bool:
        """the filters the summaries use: library, type and seasons"""
        return item.kind in self.kinds() and (self.library is None or item.library == self.library)


def parse_filters(request: Request) -> Filters:
    params = request.query_params
    filters = Filters(library=params.get("library") or None)
    if params.get("type"):
        if params["type"] not in TYPES:
            raise HTTPException(status_code=422, detail=f"type must be one of {', '.join(TYPES)}")
        filters.type = params["type"]
    if params.get("watch"):
        if params["watch"] not in WATCH:
            raise HTTPException(status_code=422, detail=f"watch must be one of {', '.join(WATCH)}")
        filters.watch = params["watch"]
    if params.get("sort"):
        if params["sort"] not in SORTS:
            raise HTTPException(status_code=422, detail=f"sort must be one of {', '.join(SORTS)}")
        filters.sort = params["sort"]
    if params.get("days"):
        try:
            filters.days = int(params["days"])
        except ValueError:
            filters.days = -1
        if filters.days < 0:
            raise HTTPException(status_code=422, detail="days must be a whole number of days")
    if requester := params.get("requester"):
        if requester not in (ANY_REQUESTER, NO_REQUESTER) and not requester.isdigit():
            raise HTTPException(
                status_code=422, detail=f"requester must be a requester's id, {ANY_REQUESTER} or {NO_REQUESTER}"
            )
        filters.requester = requester
    filters.seasons = params.get("seasons") in ("1", "true", "on")
    filters.reverse = params.get("reverse") in ("1", "true", "on")
    return filters


# ---------------------------------------------------------------------------
# requesters, from the synced seerr requests
# ---------------------------------------------------------------------------


@dataclass
class Requesters:
    """who requested each movie and show, by tmdb/tvdb id, oldest request first"""

    movies: dict[int, list[MediaRequest]] = field(default_factory=lambda: defaultdict(list))
    shows_tvdb: dict[int, list[MediaRequest]] = field(default_factory=lambda: defaultdict(list))
    shows_tmdb: dict[int, list[MediaRequest]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def load(cls, session: Session) -> "Requesters":
        found = cls()
        query = select(MediaRequest).where(MediaRequest.status != "declined").order_by(MediaRequest.requested_at)
        for r in session.scalars(query):
            if r.media_type == "movie" and r.tmdb_id:
                found.movies[r.tmdb_id].append(r)
            elif r.media_type == "tv":
                if r.tvdb_id:
                    found.shows_tvdb[r.tvdb_id].append(r)
                elif r.tmdb_id:
                    found.shows_tmdb[r.tmdb_id].append(r)
        return found

    def of(self, item: LibraryItem) -> list[tuple[int, str]]:
        """(seerr id, name) of who asked for it. A season's are those who asked for that
        season, else for any of the show"""
        if item.kind == "movie":
            found = self.movies.get(item.tmdb_id, []) if item.tmdb_id else []
        else:
            found = (self.shows_tvdb.get(item.tvdb_id, []) if item.tvdb_id else []) + (
                self.shows_tmdb.get(item.tmdb_id, []) if item.tmdb_id else []
            )
            if item.kind == "season":
                found = [r for r in found if not r.seasons or item.season in r.seasons] or found
        return list(dict.fromkeys((r.requester_id, r.requester) for r in found))


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


def days_since(when: datetime | None, now: datetime) -> int | None:
    return (now - when).days if when else None


def state(item: LibraryItem, filters: Filters, now: datetime) -> str:
    """never (not played, in plex at least the days), stale (last played before them),
    recent (played since), new (not played, newer)"""
    cutoff = now - timedelta(days=filters.days)
    if not item.plays:
        return "never" if item.added_at is not None and item.added_at <= cutoff else "new"
    return "stale" if item.last_played_at is not None and item.last_played_at <= cutoff else "recent"


STATES = {"never": "Never watched", "stale": "Stale", "recent": "Watched lately", "new": "New, not watched"}
WATCH_STATES = {
    "unwatched": {"never", "stale"},
    "never": {"never"},
    "stale": {"stale"},
    "recent": {"recent"},
    "all": set(STATES),
}


@dataclass
class Row:
    item: LibraryItem
    state: str
    requesters: list[tuple[int, str]]

    @property
    def label(self) -> str:
        i = self.item
        title = f"{i.title} ({i.year})" if i.year else i.title
        if i.kind == "season":
            return f"{title} – {'Specials' if i.season == 0 else f'Season {i.season}'}"
        return title

    @property
    def idle_since(self) -> datetime | None:
        return self.item.last_played_at or self.item.added_at


def sort_rows(rows: list[Row], sort: str, reverse: bool) -> list[Row]:
    oldest = datetime.min
    keys = {
        "idle": lambda r: (r.idle_since or oldest, r.label.casefold()),
        "title": lambda r: r.label.casefold(),
        "added": lambda r: (r.item.added_at or oldest, r.label.casefold()),
        # never played first
        "last_played": lambda r: (r.item.last_played_at or oldest, r.item.added_at or oldest),
        "plays": lambda r: (-r.item.plays, r.label.casefold()),
        # no requester last
        "requester": lambda r: (not r.requesters, ", ".join(n for _, n in r.requesters).casefold(), r.label.casefold()),
    }
    return sorted(rows, key=keys[sort], reverse=reverse)


def load_rows(request: Request, filters: Filters, now: datetime) -> tuple[list[Row], list[Row], list[str]]:
    """(rows in scope, of those the ones every filter matches sorted, every library name)"""
    with Session(request.app.state.engine) as session:
        items = list(session.scalars(select(LibraryItem)))
        requesters = Requesters.load(session)
    libraries = sorted({i.library for i in items}, key=str.casefold)
    scoped = [Row(i, state(i, filters, now), requesters.of(i)) for i in items if filters.in_scope(i)]
    wanted = WATCH_STATES[filters.watch]
    shown = [r for r in scoped if r.state in wanted and requested_by(r, filters.requester)]
    return scoped, sort_rows(shown, filters.sort, filters.reverse), libraries


def requested_by(row: Row, requester: str | None) -> bool:
    """the requester filter: None either way, ANY_REQUESTER by someone, NO_REQUESTER by
    no one, else by that seerr id"""
    if requester is None:
        return True
    if requester == ANY_REQUESTER:
        return bool(row.requesters)
    if requester == NO_REQUESTER:
        return not row.requesters
    return any(str(id) == requester for id, _ in row.requesters)


def summaries(rows: list[Row]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """never watched, stale and every item, per requester (an item asked for by two counts
    for both; no request last) and per library"""

    def count(groups: dict[Any, dict[str, Any]], key: Any, name: str, row: Row) -> None:
        g = groups.setdefault(key, {"key": key, "name": name, "items": 0, "never": 0, "stale": 0})
        g["items"] += 1
        if row.state in ("never", "stale"):
            g[row.state] += 1

    by_requester: dict[Any, dict[str, Any]] = {}
    by_library: dict[Any, dict[str, Any]] = {}
    for row in rows:
        for id, name in row.requesters or [(NO_REQUESTER, "No request")]:
            count(by_requester, str(id), name, row)
        count(by_library, row.item.library, row.item.library, row)
    requesters = sorted(
        by_requester.values(), key=lambda g: (g["key"] == NO_REQUESTER, -(g["never"] + g["stale"]), g["name"].casefold())
    )
    return requesters, sorted(by_library.values(), key=lambda g: g["name"].casefold())


def row_dict(row: Row, now: datetime) -> dict[str, Any]:
    i = row.item
    return {
        "type": i.kind,
        "library": i.library,
        "title": i.title,
        "year": i.year,
        "season": i.season,
        "rating_key": i.rating_key,
        "tmdb_id": i.tmdb_id,
        "tvdb_id": i.tvdb_id,
        "episodes": i.episodes,
        "added_at": i.added_at.isoformat() if i.added_at else None,
        "days_since_added": days_since(i.added_at, now),
        "plays": i.plays,
        "viewers": i.viewers,
        "last_played_at": i.last_played_at.isoformat() if i.last_played_at else None,
        "days_since_played": days_since(i.last_played_at, now),
        "last_viewer": i.last_viewer,
        "state": row.state,
        "requesters": [{"id": id, "name": name} for id, name in row.requesters],
    }


def stats_sync(request: Request) -> StatsSync:
    return request.app.state.stats


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@router.get("/library", response_class=HTMLResponse)
def library_page(request: Request) -> HTMLResponse:
    filters = parse_filters(request)
    now = utc_now()
    scoped, rows, libraries = load_rows(request, filters, now)
    by_requester, by_library = summaries(scoped)
    requesters = sorted(
        {id: name for r in scoped for id, name in r.requesters}.items(), key=lambda u: u[1].casefold()
    )
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "user": request.state.user,
            "status": sync_status(request.app.state.engine, LIBRARY_STATUS),
            "syncing": stats_sync(request).running,
            "missing": missing_connections(request, "tautulli", "plex"),
            "filters": filters,
            "rows": rows,
            "limit": None if request.query_params.get("all") == "1" else SHOWN,
            "scoped": len(scoped),
            "by_requester": by_requester,
            "by_library": by_library,
            "libraries": libraries,
            "requesters": requesters,
            "now": now,
            "days_since": days_since,
            "ANY_REQUESTER": ANY_REQUESTER,
            "NO_REQUESTER": NO_REQUESTER,
            "STATES": STATES,
            "WATCH": WATCH,
            "TYPES": TYPES,
            "SORTS": SORTS,
        },
    )


@router.post("/library/sync")
def sync_now(request: Request) -> RedirectResponse:
    stats_sync(request).sync_now()
    return RedirectResponse("/library", status_code=303)


CSV_COLUMNS = [
    "library", "type", "title", "year", "season", "episodes", "added", "days_since_added",
    "plays", "viewers", "last_played", "days_since_played", "last_viewer", "state", "requested_by",
]


def csv_cell(value: object) -> object:
    """a text cell a spreadsheet would read as a formula, quoted (titles come from plex)"""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@router.get("/library/export.csv")
def export_csv(request: Request) -> Response:
    """every row the filters match, not just the first ones the page shows"""
    filters = parse_filters(request)
    now = utc_now()
    _, rows, _ = load_rows(request, filters, now)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        i = row.item
        values = [
            i.library, i.kind, i.title, i.year, i.season, i.episodes,
            i.added_at.date().isoformat() if i.added_at else "", days_since(i.added_at, now),
            i.plays, i.viewers,
            i.last_played_at.date().isoformat() if i.last_played_at else "", days_since(i.last_played_at, now),
            i.last_viewer, STATES[row.state], "; ".join(name for _, name in row.requesters),
        ]
        writer.writerow(["" if v is None else csv_cell(v) for v in values])
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="library.csv"'},
    )


@router.get("/api/library")
def api_library(request: Request) -> JSONResponse:
    filters = parse_filters(request)
    now = utc_now()
    scoped, rows, _ = load_rows(request, filters, now)
    by_requester, by_library = summaries(scoped)
    status = sync_status(request.app.state.engine, LIBRARY_STATUS)
    return JSONResponse(
        {
            "synced_at": status.finished_at.isoformat() if status.finished_at else None,
            "sync_error": status.error,
            "days": filters.days,
            "by_requester": by_requester,
            "by_library": by_library,
            "items": [row_dict(r, now) for r in rows],
        }
    )
