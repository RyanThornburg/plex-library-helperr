"""stats.py - request stats: who requested what in Seerr, and whether they watched it.

StatsSync (a web.sync.BackgroundSync) copies Tautulli's plays into the database (all of them the first time, then
what's new) and rewrites the Seerr requests with what Plex has of each and how much the
requester played, every SYNC_INTERVAL and when asked. It runs beside the JobRunner, so a
long first sync never holds up movers. It only reads from the services"""

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
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
from tautulli import HISTORY_PAGE, PLAYS_OVERLAP, TautulliRefused, from_timestamp, history_rows, tautulli_after, tautulli_get
from web.db import MediaRequest, Play
from web.jobs import utc_now
from web.lookups import redact
from web.seerr import SeerrRefused, seerr_get
from web.sync import BackgroundSync, SyncError, SyncStatus, as_int, as_text, save_status

logger = logging.getLogger(__name__)

SYNC_INTERVAL = timedelta(hours=1)
TIMEOUT = 60
SEERR_PAGE = 100
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


# ---------------------------------------------------------------------------
# tautulli plays
# ---------------------------------------------------------------------------


def _tautulli(config: TautulliConfig, cmd: str, **params: object) -> Any:
    return tautulli_get(config.url, config.apikey, cmd, timeout=TIMEOUT, **params)


# plex's agents before late 2021: com.plexapp.agents.thetvdb://121361/6/1?lang=en
LEGACY_AGENT = "com.plexapp.agents."


def normalize_guid(guid: str) -> str:
    """a legacy agent's guid without its ?lang=, so request_guids can build it"""
    return guid.split("?", 1)[0] if guid.startswith(LEGACY_AGENT) else guid


def play_from_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """a Play's columns from a get_history row, None for a play still in progress (no id
    yet) or a row it can't read"""
    history_id = as_int(row.get("id"))
    started_at = from_timestamp(row.get("started") or row.get("date"))
    user_id = as_int(row.get("user_id"))
    if history_id is None or started_at is None or user_id is None:
        return None
    return {
        "id": history_id,
        "started_at": started_at,
        "stopped_at": from_timestamp(row.get("stopped")),
        "user_id": user_id,
        "username": str(row.get("user") or "")[:255],
        "media_type": str(row.get("media_type") or "")[:16],
        "rating_key": as_int(row.get("rating_key")),
        "grandparent_rating_key": as_int(row.get("grandparent_rating_key")),
        "season": as_int(row.get("parent_media_index")) if row.get("media_type") == "episode" else None,
        "episode": as_int(row.get("media_index")) if row.get("media_type") == "episode" else None,
        "guid": normalize_guid(str(row.get("guid") or ""))[:255],
        "title": str(row.get("full_title") or row.get("title") or "")[:500],
        "percent_complete": as_int(row.get("percent_complete")) or 0,
        "watched": row.get("watched_status") == 1,
        "play_duration": as_int(row.get("play_duration")),
        "transcode_decision": as_text(row.get("transcode_decision"), 32),
        "platform": as_text(row.get("platform"), 64),
        "product": as_text(row.get("product"), 64),
        "player": as_text(row.get("player"), 255),
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
        "length": HISTORY_PAGE,
    }
    if last is not None:
        params["after"] = tautulli_after(last - PLAYS_OVERLAP)
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
        if len(page) < HISTORY_PAGE:
            break
        start += HISTORY_PAGE
        if start % (HISTORY_PAGE * 20) == 0:
            total = data.get("recordsFiltered") if isinstance(data, dict) else None
            logger.info("STATS\t %s of %s plays read", read, total or "?")
    return read


# ---------------------------------------------------------------------------
# seerr requests
# ---------------------------------------------------------------------------


def _seerr(config: SeerrConfig, path: str, **params: object) -> Any:
    return seerr_get(config.url, config.apikey, path, timeout=TIMEOUT, **params)


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
    request_id = as_int(raw.get("id"))
    user_id = as_int(user.get("id"))
    requested_at = _iso_time(raw.get("createdAt"))
    media_type = raw.get("type") or media.get("mediaType")
    if request_id is None or user_id is None or requested_at is None or media_type not in ("movie", "tv"):
        return None
    is_4k = bool(raw.get("is4k"))
    seasons = tuple(
        sorted(
            n
            for s in raw.get("seasons") or []
            if isinstance(s, dict) and (n := as_int(s.get("seasonNumber"))) is not None
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
        tmdb_id=as_int(media.get("tmdbId")),
        tvdb_id=as_int(media.get("tvdbId")),
        status=REQUEST_STATUS.get(as_int(raw.get("status")) or 0, str(raw.get("status"))),
        media_status=MEDIA_STATUS.get(
            as_int(media.get("status4k" if is_4k else "status")) or 0, "unknown"
        ),
        is_4k=is_4k,
        seasons=seasons,
        requested_at=requested_at,
        available_at=_iso_time(media.get("mediaAddedAt")),
        requester=Requester(
            seerr_id=user_id,
            name=str(name)[:255],
            plex_id=as_int(user.get("plexId")),
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
            if as_int(u.get("user_id")) == requester.plex_id:
                return requester.plex_id
    for field, value in (("username", requester.plex_username), ("email", requester.email)):
        if not value:
            continue
        for u in users:
            if str(u.get(field) or "").casefold() == value.casefold():
                return as_int(u.get("user_id"))
    return None


# ---------------------------------------------------------------------------
# plex: what each request has in plex, by guid
# ---------------------------------------------------------------------------


# episodes plex lists per request: a show's own list (show.episodes(), plexapi's page
# size), and a whole library's, read when that's fewer requests
SHOW_PAGE = 100
SECTION_PAGE = 1000


@dataclass(frozen=True)
class EpisodeRef:
    """what wanted() reads of an episode, kept instead of plexapi's objects (which hold
    their page's xml): a sync can read every episode of a library"""

    parentIndex: int | None
    index: int | None
    guid: str | None


def _ref(episode: Any) -> EpisodeRef:
    return EpisodeRef(episode.parentIndex, episode.index, episode.guid)


class PlexIndex:
    """plex's movies and shows by tmdb/tvdb guid, read once per sync"""

    def __init__(self, plex: Any, types: tuple[str, ...] = ("movie", "show")):
        self.by_guid: dict[tuple[str, str], list[Any]] = defaultdict(list)
        # show sections, and each show's section and episode count, to choose how
        # load_episodes() reads them
        self._sections: dict[Any, Any] = {}
        self._show_section: dict[Any, Any] = {}
        self._leaves: dict[Any, int] = {}
        for section in plex.library.sections():
            if section.type not in types:
                continue
            for item in section.search(includeGuids=True):
                for guid in item.guids:
                    self.by_guid[(section.type, guid.id)].append(item)
                if section.type == "show":
                    self._sections[section.key] = section
                    self._show_section[item.ratingKey] = section.key
                    self._leaves[item.ratingKey] = getattr(item, "leafCount", None) or 0
        self._episodes: dict[Any, list[EpisodeRef]] = {}

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

    def load_episodes(self, shows: Iterable[Any]) -> None:
        """read these shows' episodes in as few requests as plex allows: each show's
        own list is a request per SHOW_PAGE episodes, its library's whole list one per
        SECTION_PAGE episodes of every show in it. The cheaper wins per library; shows
        left out are read one at a time by episodes()"""
        by_section: dict[Any, set[Any]] = defaultdict(set)
        for show in shows:
            if show.ratingKey not in self._episodes and show.ratingKey in self._show_section:
                by_section[self._show_section[show.ratingKey]].add(show.ratingKey)
        for key, wanted_keys in by_section.items():
            own = sum(-(-max(self._leaves[k], 1) // SHOW_PAGE) for k in wanted_keys)
            in_section = [k for k, s in self._show_section.items() if s == key]
            whole = -(-max(sum(self._leaves[k] for k in in_section), 1) // SECTION_PAGE)
            if whole < own:
                self._read_section(self._sections[key], in_section)

    def _read_section(self, section: Any, show_keys: list[Any]) -> None:
        found: dict[Any, list[EpisodeRef]] = {k: [] for k in show_keys}
        start = 0
        while True:
            page = section.search(
                libtype="episode", container_start=start, container_size=SECTION_PAGE,
                maxresults=SECTION_PAGE,
            )
            for episode in page:
                found.setdefault(episode.grandparentRatingKey, []).append(_ref(episode))
            if len(page) < SECTION_PAGE:
                break
            start += SECTION_PAGE
        self._episodes.update(found)

    def episodes(self, show: Any) -> list[EpisodeRef]:
        if show.ratingKey not in self._episodes:
            self._episodes[show.ratingKey] = [_ref(e) for e in show.episodes()]
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
            raise SyncError("needs Tautulli and Plex on Settings → Connections")
        try:
            sync_plays(engine, tautulli)
            index = PlexIndex(PlexServer(plex.baseurl, plex.token, timeout=TIMEOUT), ("movie",))
        except Exception as e:
            # requests errors hold urls, and tautulli's key is a query param
            raise SyncError(redact(f"{type(e).__name__}: {e}", tautulli.apikey, plex.token)) from None
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
    except requests.exceptions.RequestException, SeerrRefused, ValueError:
        return f"TMDB {request.tmdb_id}", None
    title = details.get("title") or details.get("name") or f"TMDB {request.tmdb_id}"
    date = details.get("releaseDate") or details.get("firstAirDate") or ""
    return str(title)[:500], as_int(date[:4]) if date else None


def sync_requests(
    engine: Engine, seerr: SeerrConfig, tautulli: TautulliConfig, plex: PlexConfig
) -> int:
    """rewrite media_requests from seerr, plex and the copied plays. Returns the count"""
    raw = seerr_requests(seerr)
    users = _tautulli(tautulli, "get_users")
    index = PlexIndex(PlexServer(plex.baseurl, plex.token, timeout=TIMEOUT))
    with Session(engine) as session:
        known = {r.id: (r.title, r.year) for r in session.scalars(select(MediaRequest))}

    parsed: list[ParsedRequest] = []
    for item in raw:
        request = parse_request(item)
        if request is None:
            logger.warning("STATS\t Skipping a Seerr request it can't read (id %s)", item.get("id"))
            continue
        parsed.append(request)
    # every requested show's episodes at once, a library at a time when that's cheaper
    index.load_episodes(item for r in parsed if r.media_type == "tv" for item in index.items(r))

    found: list[tuple[ParsedRequest, int | None, Wanted, str, int | None]] = []
    for request in parsed:
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
            raise SyncError(f"Needs {', '.join(missing)} on Settings → Connections")
        status.requests = sync_requests(
            engine, settings.seerr, settings.tautulli, settings.plex  # type: ignore[arg-type]
        )
        logger.info(
            "STATS\t Synced %s plays and %s requests", status.plays_read, status.requests
        )
    except (SyncError, TautulliRefused, SeerrRefused) as e:
        status.error = str(e)
        logger.info("STATS\t Not synced: %s", e)
    except Exception as e:
        # requests errors hold urls, and tautulli's key is a query param
        status.error = redact(f"{type(e).__name__}: {e}", *secrets)
        logger.error("STATS\t Sync failed: %s", status.error)
    status.finished_at = utc_now()
    save_status(engine, status, STATUS_SETTING)
    return status


class StatsSync(BackgroundSync):
    """the requests sync: Tautulli's plays and Seerr's requests, every SYNC_INTERVAL"""

    interval = SYNC_INTERVAL
    status_key = STATUS_SETTING
    thread_name = "stats-sync"

    def sync(self, settings: ConfigSettings) -> SyncStatus:
        return sync(self.engine, settings)
