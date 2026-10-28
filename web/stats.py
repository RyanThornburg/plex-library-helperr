"""stats.py - request stats: who requested what in Seerr, and whether they watched it.

StatsSync copies Tautulli's plays into the database (all of them the first time, then
what's new) and rewrites the Seerr requests with what Plex has of each and how much the
requester played, every SYNC_INTERVAL and when asked. It runs beside the JobRunner, so a
long first sync never holds up movers. It only reads from the services"""

import json
import logging
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from plexapi.server import PlexServer
from sqlalchemy import Engine, delete, func, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from config.models import ConfigSettings, PlexConfig, SeerrConfig, TautulliConfig
from movie_remover import PlayLookup, RadarrMovie
from watch_history import history_rows
from web.db import AppSetting, MediaRequest, Play
from web.jobs import utc_now
from web.lookups import redact

logger = logging.getLogger(__name__)

SYNC_INTERVAL = timedelta(hours=1)
TIMEOUT = 60
TAUTULLI_PAGE = 1000
SEERR_PAGE = 100
# plays in progress have no history id yet, so each sync reads the last day again
PLAYS_OVERLAP = timedelta(days=1)
# rows per insert, under sqlite's limit on variables
BATCH = 500
STATUS_SETTING = "stats_sync"

REQUEST_STATUS = {1: "pending", 2: "approved", 3: "declined", 4: "failed", 5: "completed"}
MEDIA_STATUS = {
    1: "unknown",
    2: "pending",
    3: "processing",
    4: "partially available",
    5: "available",
    6: "blocklisted",
    7: "deleted",
}


class StatsError(Exception):
    """a sync that can't run or failed, the message is shown on the requests page"""


# ---------------------------------------------------------------------------
# tautulli plays
# ---------------------------------------------------------------------------


def _utc(timestamp: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(timestamp), timezone.utc).replace(tzinfo=None)  # type: ignore[arg-type]
    except TypeError, ValueError, OverflowError:
        return None


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


def _text(value: object, length: int) -> str | None:
    return str(value)[:length] if value not in (None, "") else None


def _tautulli(config: TautulliConfig, cmd: str, **params: object) -> Any:
    response = requests.get(
        f"{config.url.rstrip('/')}/api/v2",
        params={"apikey": config.apikey, "cmd": cmd, **params},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()["response"]
    if body.get("result") != "success":
        raise StatsError(f"Tautulli {cmd} failed: {body.get('message') or 'unknown error'}")
    return body["data"]


# plex's agents before late 2021: com.plexapp.agents.thetvdb://121361/6/1?lang=en
LEGACY_AGENT = "com.plexapp.agents."


def normalize_guid(guid: str) -> str:
    """a legacy agent's guid without its ?lang=, so request_guids can build it"""
    return guid.split("?", 1)[0] if guid.startswith(LEGACY_AGENT) else guid


def play_from_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """a Play's columns from a get_history row, None for a play still in progress (no id
    yet) or a row it can't read"""
    history_id = _int(row.get("id"))
    started_at = _utc(row.get("started") or row.get("date"))
    user_id = _int(row.get("user_id"))
    if history_id is None or started_at is None or user_id is None:
        return None
    return {
        "id": history_id,
        "started_at": started_at,
        "stopped_at": _utc(row.get("stopped")),
        "user_id": user_id,
        "username": str(row.get("user") or "")[:255],
        "media_type": str(row.get("media_type") or "")[:16],
        "rating_key": _int(row.get("rating_key")),
        "grandparent_rating_key": _int(row.get("grandparent_rating_key")),
        "season": _int(row.get("parent_media_index")) if row.get("media_type") == "episode" else None,
        "episode": _int(row.get("media_index")) if row.get("media_type") == "episode" else None,
        "guid": normalize_guid(str(row.get("guid") or ""))[:255],
        "title": str(row.get("full_title") or row.get("title") or "")[:500],
        "percent_complete": _int(row.get("percent_complete")) or 0,
        "watched": row.get("watched_status") == 1,
        "play_duration": _int(row.get("play_duration")),
        "transcode_decision": _text(row.get("transcode_decision"), 32),
        "platform": _text(row.get("platform"), 64),
        "product": _text(row.get("product"), 64),
        "player": _text(row.get("player"), 255),
    }


def _upsert_plays(session: Session, plays: list[dict[str, Any]]) -> None:
    for start in range(0, len(plays), BATCH):
        stmt = sqlite_insert(Play).values(plays[start : start + BATCH])
        stmt = stmt.on_conflict_do_update(
            index_elements=[Play.id],
            set_={c.name: stmt.excluded[c.name] for c in Play.__table__.columns if c.name != "id"},
        )
        session.execute(stmt)


def sync_plays(engine: Engine, config: TautulliConfig) -> int:
    """copy tautulli's history, oldest first so new plays don't shift the pages. Returns
    the plays read"""
    with Session(engine) as session:
        last = session.scalar(select(func.max(Play.started_at)))
    params: dict[str, object] = {
        "grouping": 0,
        "order_column": "date",
        "order_dir": "asc",
        "length": TAUTULLI_PAGE,
    }
    if last is not None:
        params["after"] = (last - PLAYS_OVERLAP).date().isoformat()
    read = 0
    start = 0
    while True:
        data = _tautulli(config, "get_history", start=start, **params)
        # an unexpected answer fails the sync rather than leaving the plays stale unseen
        page = history_rows(data)
        plays = [p for p in map(play_from_row, page) if p]
        if plays:
            with Session(engine) as session:
                _upsert_plays(session, plays)
                session.commit()
        read += len(plays)
        if len(page) < TAUTULLI_PAGE:
            break
        start += TAUTULLI_PAGE
        if start % (TAUTULLI_PAGE * 20) == 0:
            total = data.get("recordsFiltered") if isinstance(data, dict) else None
            logger.info("STATS\t %s of %s plays read", read, total or "?")
    return read


# ---------------------------------------------------------------------------
# seerr requests
# ---------------------------------------------------------------------------


def _seerr(config: SeerrConfig, path: str, **params: object) -> Any:
    response = requests.get(
        f"{config.url.rstrip('/')}/api/v1/{path}",
        params=params,
        headers={"X-Api-Key": config.apikey},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def seerr_requests(config: SeerrConfig) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    skip = 0
    while True:
        body = _seerr(config, "request", take=SEERR_PAGE, skip=skip, filter="all", sort="added")
        page = body.get("results", []) if isinstance(body, dict) else []
        results.extend(page)
        if len(page) < SEERR_PAGE:
            return results
        skip += SEERR_PAGE


def _iso_time(value: object) -> datetime | None:
    """an iso time (seerr's are 2026-10-24T15:03:58.000Z) as naive utc"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


@dataclass(frozen=True)
class Requester:
    seerr_id: int
    name: str
    plex_id: int | None
    plex_username: str | None
    email: str | None


@dataclass(frozen=True)
class ParsedRequest:
    id: int
    media_type: str  # movie or tv
    tmdb_id: int | None
    tvdb_id: int | None
    status: str
    media_status: str
    is_4k: bool
    seasons: tuple[int, ...]
    requested_at: datetime
    available_at: datetime | None
    requester: Requester


def parse_request(raw: Mapping[str, Any]) -> ParsedRequest | None:
    """a seerr request's fields, None when it lacks what stats need"""
    media = raw.get("media") if isinstance(raw.get("media"), dict) else {}
    user = raw.get("requestedBy") if isinstance(raw.get("requestedBy"), dict) else {}
    request_id = _int(raw.get("id"))
    user_id = _int(user.get("id"))
    requested_at = _iso_time(raw.get("createdAt"))
    media_type = raw.get("type") or media.get("mediaType")
    if request_id is None or user_id is None or requested_at is None or media_type not in ("movie", "tv"):
        return None
    is_4k = bool(raw.get("is4k"))
    seasons = tuple(
        sorted(
            n
            for s in raw.get("seasons") or []
            if isinstance(s, dict) and (n := _int(s.get("seasonNumber"))) is not None
        )
    )
    name = (
        user.get("displayName")
        or user.get("plexUsername")
        or user.get("username")
        or user.get("email")
        or f"user {user_id}"
    )
    return ParsedRequest(
        id=request_id,
        media_type=media_type,
        tmdb_id=_int(media.get("tmdbId")),
        tvdb_id=_int(media.get("tvdbId")),
        status=REQUEST_STATUS.get(_int(raw.get("status")) or 0, str(raw.get("status"))),
        media_status=MEDIA_STATUS.get(
            _int(media.get("status4k" if is_4k else "status")) or 0, "unknown"
        ),
        is_4k=is_4k,
        seasons=seasons,
        requested_at=requested_at,
        available_at=_iso_time(media.get("mediaAddedAt")),
        requester=Requester(
            seerr_id=user_id,
            name=str(name)[:255],
            plex_id=_int(user.get("plexId")),
            plex_username=user.get("plexUsername") or None,
            email=user.get("email") or None,
        ),
    )


def match_user(requester: Requester, users: Iterable[Mapping[str, Any]]) -> int | None:
    """the requester's plex account (tautulli user_id): by plex id, then plex username,
    then email"""
    users = list(users)
    if requester.plex_id is not None:
        for u in users:
            if _int(u.get("user_id")) == requester.plex_id:
                return requester.plex_id
    for field, value in (("username", requester.plex_username), ("email", requester.email)):
        if not value:
            continue
        for u in users:
            if str(u.get(field) or "").casefold() == value.casefold():
                return _int(u.get("user_id"))
    return None


# ---------------------------------------------------------------------------
# plex: what each request has in plex, by guid
# ---------------------------------------------------------------------------


class PlexIndex:
    """plex's movies and shows by tmdb/tvdb guid, read once per sync"""

    def __init__(self, plex: Any, types: tuple[str, ...] = ("movie", "show")):
        self.plex = plex
        self.by_guid: dict[tuple[str, str], list[Any]] = defaultdict(list)
        for section in plex.library.sections():
            if section.type not in types:
                continue
            for item in section.search(includeGuids=True):
                for guid in item.guids:
                    self.by_guid[(section.type, guid.id)].append(item)
        self._episodes: dict[Any, list[Any]] = {}

    def items(self, request: ParsedRequest) -> list[Any]:
        if request.media_type == "movie":
            keys = [("movie", f"tmdb://{request.tmdb_id}")] if request.tmdb_id else []
        else:
            keys = [
                ("show", f"{scheme}://{value}")
                for scheme, value in (("tvdb", request.tvdb_id), ("tmdb", request.tmdb_id))
                if value
            ]
        for key in keys:
            if found := self.by_guid.get(key):
                return found
        return []

    def episodes(self, show: Any) -> list[Any]:
        if show.ratingKey not in self._episodes:
            self._episodes[show.ratingKey] = show.episodes()
        return self._episodes[show.ratingKey]


def _external_ids(item: Any, scheme: str) -> list[str]:
    """an item's ids from its tmdb://, imdb:// or tvdb:// guids"""
    return [g.id.split("://", 1)[1] for g in item.guids if g.id.startswith(f"{scheme}://")]


@dataclass
class Wanted:
    """what plays count for a request, and what plex has of it now"""

    media_type: str
    # guid -> "movie" or (season, episode), of what plex has now. Plays from before plex's
    # new agents have legacy guids, built from the items' tmdb/imdb/tvdb ids
    guids: dict[str, Any]
    # a show's rating keys and tvdb ids: plays of its episodes, including ones plex no
    # longer has (removed, or replaced and renumbered)
    show_keys: set[int]
    tvdb_ids: set[str]
    # requested seasons, empty is every numbered season
    seasons: tuple[int, ...]

    @property
    def items(self) -> set[Any]:
        return set(self.guids.values())

    def counts(self, season: int) -> bool:
        return season in self.seasons if self.seasons else season != 0


def wanted(request: ParsedRequest, items: list[Any], index: PlexIndex) -> Wanted:
    """a request's guids from its plex items. A movie or show in two libraries counts once"""
    result = Wanted(request.media_type, {}, set(), set(), request.seasons)
    if request.media_type == "movie":
        for item in items:
            if item.guid:
                result.guids[item.guid] = "movie"
            for scheme, agent in (("tmdb", "themoviedb"), ("imdb", "imdb")):
                for external_id in _external_ids(item, scheme):
                    result.guids[f"{LEGACY_AGENT}{agent}://{external_id}"] = "movie"
        return result
    for show in items:
        result.show_keys.add(int(show.ratingKey))
        tvdb_ids = _external_ids(show, "tvdb")
        result.tvdb_ids.update(tvdb_ids)
        for episode in index.episodes(show):
            season, number = episode.parentIndex, episode.index
            if season is None or number is None or not episode.guid or not result.counts(season):
                continue
            result.guids[episode.guid] = (season, number)
            for tvdb_id in tvdb_ids:
                result.guids[f"{LEGACY_AGENT}thetvdb://{tvdb_id}/{season}/{number}"] = (season, number)
    return result


def movie_guids(items: list[Any], tmdb_id: int | None, imdb_id: str | None) -> set[str]:
    """the guids a movie's plays can have: its plex items' guids, and the legacy agents'
    guids of its tmdb/imdb ids (radarr's, and the items')"""
    guids = {item.guid for item in items if item.guid}
    ids = {"tmdb": {str(tmdb_id)} if tmdb_id else set(), "imdb": {imdb_id} if imdb_id else set()}
    for item in items:
        for scheme in ids:
            ids[scheme].update(_external_ids(item, scheme))
    for scheme, agent in (("tmdb", "themoviedb"), ("imdb", "imdb")):
        guids.update(f"{LEGACY_AGENT}{agent}://{external_id}" for external_id in ids[scheme])
    return guids


def movie_play_lookup(engine: Engine, settings: ConfigSettings) -> PlayLookup:
    """movie_remover's PlayLookup, from the plays table. It syncs new plays from Tautulli
    first, so a play from the last hour counts. Movies plex doesn't have are left out
    (their plays can't all be found), so removers that check plays keep them"""

    def lookup(movies: list[RadarrMovie]) -> dict[int, list[datetime]]:
        tautulli, plex = settings.tautulli, settings.plex
        if tautulli is None or plex is None:
            raise StatsError("needs Tautulli and Plex on Settings → Connections")
        try:
            sync_plays(engine, tautulli)
            index = PlexIndex(PlexServer(plex.baseurl, plex.token, timeout=TIMEOUT), ("movie",))
        except Exception as e:
            # requests errors hold urls, and tautulli's key is a query param
            raise StatsError(redact(f"{type(e).__name__}: {e}", tautulli.apikey, plex.token)) from None
        found: dict[int, list[datetime]] = {}
        with Session(engine) as session:
            for movie in movies:
                keys = [("movie", f"tmdb://{movie.tmdb_id}")] if movie.tmdb_id else []
                keys += [("movie", f"imdb://{movie.imdb_id}")] if movie.imdb_id else []
                items = next((index.by_guid[k] for k in keys if index.by_guid.get(k)), [])
                if not items:
                    logger.info("STATS\t %s isn't in Plex, its plays are unknown", movie.label)
                    continue
                guids = sorted(movie_guids(items, movie.tmdb_id, movie.imdb_id))
                found[movie.id] = sorted(
                    session.scalars(select(Play.started_at).where(Play.guid.in_(guids)))
                )
        return found

    return lookup


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlayFact:
    """what a summary needs of a play: "movie" or its (season, episode)"""

    user_id: int
    watched: bool
    started_at: datetime
    key: Any


@dataclass(frozen=True)
class Summary:
    items: int
    watched: int
    started: int
    requester_plays: int
    last_played_at: datetime | None
    other_viewers: int


def find_plays(session: Session, want: Wanted) -> list[PlayFact]:
    """the plays of a request: by guid, and a show's by rating key or legacy tvdb guid too,
    only of the seasons that count"""
    columns = (Play.id, Play.guid, Play.user_id, Play.watched, Play.started_at, Play.season, Play.episode)
    conditions = []
    guids = sorted(want.guids)
    conditions += [Play.guid.in_(guids[i : i + BATCH]) for i in range(0, len(guids), BATCH)]
    if want.show_keys:
        conditions.append(Play.grandparent_rating_key.in_(sorted(want.show_keys)))
    for tvdb_id in sorted(want.tvdb_ids):
        # a prefix as a range, so it uses the guid index (LIKE wouldn't)
        prefix = f"{LEGACY_AGENT}thetvdb://{tvdb_id}/"
        conditions.append(Play.guid.between(prefix, prefix + "\uffff"))
    rows = {}
    for condition in conditions:
        for row in session.execute(select(*columns).where(condition)):
            rows[row.id] = row
    plays = []
    for row in rows.values():
        if want.media_type == "movie":
            key: Any = "movie"
        elif row.season is not None and row.episode is not None:
            key = (row.season, row.episode)
        else:
            key = want.guids.get(row.guid)
        if key is None or (key != "movie" and not want.counts(key[0])):
            continue
        plays.append(PlayFact(row.user_id, row.watched, row.started_at, key))
    return plays


def summarize(items: set[Any], plays: Iterable[PlayFact], requester_user_id: int | None) -> Summary:
    """how much of what plex has its requester finished/played, how often they played any
    of it, and how many others played it"""
    watched: set[Any] = set()
    started: set[Any] = set()
    requester_plays = 0
    last: datetime | None = None
    others: set[int] = set()
    for play in plays:
        if requester_user_id is None or play.user_id != requester_user_id:
            others.add(play.user_id)
            continue
        requester_plays += 1
        last = play.started_at if last is None else max(last, play.started_at)
        if play.key in items:
            started.add(play.key)
            if play.watched:
                watched.add(play.key)
    return Summary(
        items=len(items),
        watched=len(watched),
        started=len(started),
        requester_plays=requester_plays,
        last_played_at=last,
        other_viewers=len(others),
    )


def _seerr_title(config: SeerrConfig, request: ParsedRequest) -> tuple[str, int | None]:
    """title and year from seerr (tmdb), for a request plex doesn't have"""
    if not request.tmdb_id:
        return f"TVDB {request.tvdb_id}", None
    try:
        details = _seerr(config, f"{request.media_type}/{request.tmdb_id}")
    except requests.exceptions.RequestException, ValueError:
        return f"TMDB {request.tmdb_id}", None
    title = details.get("title") or details.get("name") or f"TMDB {request.tmdb_id}"
    date = details.get("releaseDate") or details.get("firstAirDate") or ""
    return str(title)[:500], _int(date[:4]) if date else None


def sync_requests(
    engine: Engine, seerr: SeerrConfig, tautulli: TautulliConfig, plex: PlexConfig
) -> int:
    """rewrite media_requests from seerr, plex and the copied plays. Returns the count"""
    raw = seerr_requests(seerr)
    users = _tautulli(tautulli, "get_users")
    index = PlexIndex(PlexServer(plex.baseurl, plex.token, timeout=TIMEOUT))
    with Session(engine) as session:
        known = {r.id: (r.title, r.year) for r in session.scalars(select(MediaRequest))}

    found: list[tuple[ParsedRequest, int | None, Wanted, str, int | None]] = []
    for item in raw:
        request = parse_request(item)
        if request is None:
            logger.warning("STATS\t Skipping a Seerr request it can't read (id %s)", item.get("id"))
            continue
        items = index.items(request)
        if items:
            title, year = items[0].title, items[0].year
        elif request.id in known:
            title, year = known[request.id]
        else:
            title, year = _seerr_title(seerr, request)
        found.append(
            (request, match_user(request.requester, users), wanted(request, items, index), title, year)
        )

    now = utc_now()
    with Session(engine) as session:
        rows = []
        for request, user_id, want, title, year in found:
            summary = summarize(want.items, find_plays(session, want), user_id)
            rows.append(
                {
                    "id": request.id,
                    "media_type": request.media_type,
                    "tmdb_id": request.tmdb_id,
                    "tvdb_id": request.tvdb_id,
                    "title": str(title)[:500],
                    "year": year,
                    "status": request.status,
                    "media_status": request.media_status,
                    "is_4k": request.is_4k,
                    "seasons": list(request.seasons),
                    "requested_at": request.requested_at,
                    "available_at": request.available_at,
                    "requester_id": request.requester.seerr_id,
                    "requester": request.requester.name,
                    "requester_user_id": user_id,
                    "items": summary.items,
                    "watched": summary.watched,
                    "started": summary.started,
                    "requester_plays": summary.requester_plays,
                    "last_played_at": summary.last_played_at,
                    "other_viewers": summary.other_viewers,
                    "synced_at": now,
                }
            )
        session.execute(delete(MediaRequest))
        for start in range(0, len(rows), BATCH):
            session.execute(insert(MediaRequest), rows[start : start + BATCH])
        session.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# status and the sync thread
# ---------------------------------------------------------------------------


@dataclass
class SyncStatus:
    """the last sync, kept in app_settings so a restart shows it"""

    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    plays_read: int = 0
    requests: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
                "error": self.error,
                "plays_read": self.plays_read,
                "requests": self.requests,
            }
        )


def sync_status(engine: Engine, key: str = STATUS_SETTING) -> SyncStatus:
    with Session(engine) as session:
        setting = session.get(AppSetting, key)
    if setting is None:
        return SyncStatus()
    try:
        data = json.loads(setting.value)
        return SyncStatus(
            started_at=_iso_time(data.get("started_at")),
            finished_at=_iso_time(data.get("finished_at")),
            error=data.get("error"),
            plays_read=data.get("plays_read") or 0,
            requests=data.get("requests") or 0,
        )
    except ValueError, AttributeError:
        return SyncStatus()


def save_status(engine: Engine, status: SyncStatus, key: str = STATUS_SETTING) -> None:
    with Session(engine) as session:
        setting = session.get(AppSetting, key) or AppSetting(key=key)
        setting.value = status.to_json()
        setting.updated_at = utc_now()
        session.add(setting)
        session.commit()


def missing_connections(settings: ConfigSettings) -> list[str]:
    return [
        name
        for name, value in (("Seerr", settings.seerr), ("Tautulli", settings.tautulli), ("Plex", settings.plex))
        if value is None
    ]


def sync(engine: Engine, settings: ConfigSettings) -> SyncStatus:
    """one sync, its status saved. Never raises: the error is in the status"""
    status = SyncStatus(started_at=utc_now())
    secrets = [
        settings.seerr.apikey if settings.seerr else None,
        settings.tautulli.apikey if settings.tautulli else None,
        settings.plex.token if settings.plex else None,
    ]
    try:
        # plays first, so the slow first copy can be done before seerr is connected
        if settings.tautulli is not None:
            status.plays_read = sync_plays(engine, settings.tautulli)
        if missing := missing_connections(settings):
            raise StatsError(f"Needs {', '.join(missing)} on Settings → Connections")
        status.requests = sync_requests(
            engine, settings.seerr, settings.tautulli, settings.plex  # type: ignore[arg-type]
        )
        logger.info(
            "STATS\t Synced %s plays and %s requests", status.plays_read, status.requests
        )
    except StatsError as e:
        status.error = str(e)
        logger.info("STATS\t Not synced: %s", e)
    except Exception as e:
        # requests errors hold urls, and tautulli's key is a query param
        status.error = redact(f"{type(e).__name__}: {e}", *secrets)
        logger.error("STATS\t Sync failed: %s", status.error)
    status.finished_at = utc_now()
    save_status(engine, status)
    return status


class StatsSync:
    """syncs at start, every interval, and when sync_now() is called. Subclasses set
    their own sync, interval and status key"""

    interval = SYNC_INTERVAL
    status_key = STATUS_SETTING
    thread_name = "stats-sync"

    def __init__(self, engine: Engine, load_config: Callable[[], ConfigSettings]):
        self.engine = engine
        self.load_config = load_config
        self.running = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=self.thread_name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout)

    def sync_now(self) -> None:
        self._wake.set()

    def run_once(self) -> SyncStatus:
        self.running = True
        try:
            try:
                settings = self.load_config()
            except Exception as e:
                now = utc_now()
                status = SyncStatus(now, now, f"The settings can't be loaded: {e}")
                save_status(self.engine, status, self.status_key)
                return status
            return self.sync(settings)
        finally:
            self.running = False

    def sync(self, settings: ConfigSettings) -> SyncStatus:
        return sync(self.engine, settings)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._wake.wait(self.interval.total_seconds())
            self._wake.clear()
