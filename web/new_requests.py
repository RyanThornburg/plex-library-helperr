"""new_requests.py - adding a request through Seerr: searching, a title's details (what's
already in Plex or requested, per season), Seerr's Radarr/Sonarr servers with their
profiles, root folders and tags, its users, and the request itself (POST request).

The parsers and request_body() are pure, tests feed them Seerr's json. Seerr requests
made with the API key are its admin's, so they're approved straight away, and the
choices made here (server, profile, root folder, tags, who it's for) are honoured"""

import logging
import re
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from config.models import SeerrConfig
from web.seerr import seerr_get, seerr_post
from web.sync import as_int

logger = logging.getLogger(__name__)

TIMEOUT = 20
MEDIA_TYPES = {"movie": "Movie", "tv": "TV"}
# the arr seerr sends each type to
ARRS = {"movie": "radarr", "tv": "sonarr"}
ARR_LABELS = {"radarr": "Radarr", "sonarr": "Sonarr"}
# TMDB's "anime" keyword: seerr sends such shows to sonarr's anime folder and profile
ANIME_KEYWORD = 210024
# seerr's media statuses. Unknown has nothing, deleted was removed (both can be requested)
UNKNOWN, PENDING, PROCESSING, PARTLY, AVAILABLE, BLOCKLISTED, DELETED = range(1, 8)
OPEN = (UNKNOWN, DELETED)
# what a status means here, for tags: (word, tag class)
STATES = {
    PENDING: ("Requested", "tag-info"),
    PROCESSING: ("Processing", "tag-info"),
    PARTLY: ("Partly in Plex", "tag-info"),
    AVAILABLE: ("In Plex", ""),
    BLOCKLISTED: ("Blocklisted", "tag-warn"),
}
# a request's status: declined and completed ones don't hold seasons back (completed
# seasons are held back by being in plex)
DECLINED, COMPLETED = 3, 5
# users read a page at a time
USERS_PAGE = 100
# a tmdb image's file name, as seerr gives its path ("/abc.jpg")
POSTER_NAME = re.compile(r"^/?([A-Za-z0-9_-]{1,64}\.(?:jpg|jpeg|png))$")


class RequestInvalid(ValueError):
    """the choices can't be requested; the text says why"""


def poster_name(path: object) -> str | None:
    match = POSTER_NAME.match(path) if isinstance(path, str) else None
    return match.group(1) if match else None


def year_of(date: object) -> int | None:
    if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return None


def http_url(value: object) -> str | None:
    """a link seerr gives (plex's app url), only when it's http(s)"""
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return value
    return None


def state(status: int) -> tuple[str, str] | None:
    return STATES.get(status)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    media_type: str
    tmdb_id: int
    title: str
    year: int | None
    poster: str | None
    overview: str
    status: int = UNKNOWN
    status_4k: int = UNKNOWN

    @property
    def state(self) -> tuple[str, str] | None:
        return state(self.status)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.media_type,
            "tmdb_id": self.tmdb_id,
            "title": self.title,
            "year": self.year,
            "poster": self.poster,
            "overview": self.overview,
            "status": self.status,
            "status_4k": self.status_4k,
        }


@dataclass(frozen=True)
class SearchPage:
    results: list[Result]
    page: int
    pages: int


def media_status(raw: Mapping[str, Any]) -> tuple[int, int]:
    info = raw.get("mediaInfo") if isinstance(raw.get("mediaInfo"), dict) else {}
    return as_int(info.get("status")) or UNKNOWN, as_int(info.get("status4k")) or UNKNOWN


def parse_result(raw: Mapping[str, Any]) -> Result | None:
    """a movie or show of seerr's search, None for people and anything without an id"""
    media_type = raw.get("mediaType")
    tmdb_id = as_int(raw.get("id"))
    if media_type not in MEDIA_TYPES or tmdb_id is None:
        return None
    title = raw.get("title") if media_type == "movie" else raw.get("name")
    date = raw.get("releaseDate") if media_type == "movie" else raw.get("firstAirDate")
    status, status_4k = media_status(raw)
    return Result(
        media_type=media_type,
        tmdb_id=tmdb_id,
        title=str(title or raw.get("originalTitle") or raw.get("originalName") or f"TMDB {tmdb_id}"),
        year=year_of(date),
        poster=poster_name(raw.get("posterPath")),
        overview=str(raw.get("overview") or ""),
        status=status,
        status_4k=status_4k,
    )


def parse_search(body: object, page: int) -> SearchPage:
    body = body if isinstance(body, dict) else {}
    results = [r for raw in body.get("results") or [] if isinstance(raw, dict) and (r := parse_result(raw))]
    return SearchPage(results, as_int(body.get("page")) or page, as_int(body.get("totalPages")) or 1)


def search(config: SeerrConfig, query: str, page: int = 1) -> SearchPage:
    # seerr refuses some characters unless they're percent-encoded, and reads a + as a
    # plus: the query goes in the path, encoded like its own web page does
    path = f"search?query={quote(query, safe='')}&page={page}"
    return parse_search(seerr_get(config.url, config.apikey, path, timeout=TIMEOUT), page)


# ---------------------------------------------------------------------------
# a title's details
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Season:
    number: int
    name: str
    episodes: int
    year: int | None
    # what's there or asked for already, per copy: a STATES key, UNKNOWN when it's open
    status: int = UNKNOWN
    status_4k: int = UNKNOWN

    def state(self, is_4k: bool) -> tuple[str, str] | None:
        return state(self.status_4k if is_4k else self.status)

    def open(self, is_4k: bool) -> bool:
        return (self.status_4k if is_4k else self.status) in OPEN


@dataclass(frozen=True)
class Title:
    media_type: str
    tmdb_id: int
    title: str
    year: int | None
    poster: str | None
    overview: str
    status: int = UNKNOWN
    status_4k: int = UNKNOWN
    tvdb_id: int | None = None
    # numbered seasons, specials left out like seerr's own page
    seasons: tuple[Season, ...] = ()
    anime: bool = False
    genres: tuple[str, ...] = ()
    runtime: int | None = None
    # plex's app link to it, when seerr has one
    plex_url: str | None = None

    def state(self, is_4k: bool = False) -> tuple[str, str] | None:
        return state(self.status_4k if is_4k else self.status)

    def open_seasons(self, is_4k: bool) -> list[int]:
        return [s.number for s in self.seasons if s.open(is_4k)]

    def default_seasons(self, is_4k: bool) -> list[int]:
        """what's ticked to start with: the open seasons with episodes, like seerr's
        "all" (a season announced without episodes can still be picked)"""
        aired = [s.number for s in self.seasons if s.open(is_4k) and s.episodes]
        return aired or self.open_seasons(is_4k)

    def requestable(self, is_4k: bool) -> bool:
        if self.media_type == "tv":
            return bool(self.open_seasons(is_4k))
        return (self.status_4k if is_4k else self.status) in OPEN

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.media_type,
            "tmdb_id": self.tmdb_id,
            "tvdb_id": self.tvdb_id,
            "title": self.title,
            "year": self.year,
            "poster": self.poster,
            "overview": self.overview,
            "status": self.status,
            "status_4k": self.status_4k,
            "anime": self.anime,
            "genres": list(self.genres),
            "runtime": self.runtime,
            "plex_url": self.plex_url,
            "seasons": [
                {
                    "number": s.number,
                    "name": s.name,
                    "episodes": s.episodes,
                    "year": s.year,
                    "status": s.status,
                    "status_4k": s.status_4k,
                }
                for s in self.seasons
            ],
        }


def _requested_seasons(info: Mapping[str, Any], is_4k: bool) -> dict[int, int]:
    """season -> PENDING or PROCESSING, from seerr's requests that aren't declined or
    completed: seerr refuses to request those seasons again"""
    seasons: dict[int, int] = {}
    for request in info.get("requests") or []:
        if not isinstance(request, dict) or bool(request.get("is4k")) != is_4k:
            continue
        status = as_int(request.get("status"))
        if status in (DECLINED, COMPLETED):
            continue
        for season in request.get("seasons") or []:
            number = as_int(season.get("seasonNumber")) if isinstance(season, dict) else None
            if number is not None:
                state_now = PENDING if status == 1 else PROCESSING
                seasons[number] = max(seasons.get(number, UNKNOWN), state_now)
    return seasons


def _season_status(availability: int | None, requested: int | None) -> int:
    """a season's state: in plex (or blocklisted) wins over a request"""
    if availability is not None and availability not in OPEN:
        return availability
    return requested or UNKNOWN


def parse_title(media_type: str, raw: Mapping[str, Any]) -> Title:
    tmdb_id = as_int(raw.get("id"))
    if media_type not in MEDIA_TYPES or tmdb_id is None:
        raise ValueError("Seerr's answer has no TMDB id")
    info = raw.get("mediaInfo") if isinstance(raw.get("mediaInfo"), dict) else {}
    status, status_4k = media_status(raw)
    seasons: list[Season] = []
    if media_type == "tv":
        available = {
            n: (as_int(s.get("status")), as_int(s.get("status4k")))
            for s in info.get("seasons") or []
            if isinstance(s, dict) and (n := as_int(s.get("seasonNumber"))) is not None
        }
        requested, requested_4k = _requested_seasons(info, False), _requested_seasons(info, True)
        for s in raw.get("seasons") or []:
            number = as_int(s.get("seasonNumber")) if isinstance(s, dict) else None
            if not number:  # specials (0) and anything without a number
                continue
            now, now_4k = available.get(number, (None, None))
            seasons.append(
                Season(
                    number=number,
                    name=str(s.get("name") or f"Season {number}"),
                    episodes=as_int(s.get("episodeCount")) or 0,
                    year=year_of(s.get("airDate")),
                    status=_season_status(now, requested.get(number)),
                    status_4k=_season_status(now_4k, requested_4k.get(number)),
                )
            )
        seasons.sort(key=lambda s: s.number)
    keywords = raw.get("keywords") or []
    external = raw.get("externalIds") if isinstance(raw.get("externalIds"), dict) else {}
    date = raw.get("releaseDate") if media_type == "movie" else raw.get("firstAirDate")
    title = raw.get("title") if media_type == "movie" else raw.get("name")
    runtime = as_int(raw.get("runtime")) if media_type == "movie" else None
    return Title(
        media_type=media_type,
        tmdb_id=tmdb_id,
        title=str(title or f"TMDB {tmdb_id}"),
        year=year_of(date),
        poster=poster_name(raw.get("posterPath")),
        overview=str(raw.get("overview") or ""),
        status=status,
        status_4k=status_4k,
        tvdb_id=as_int(external.get("tvdbId")) or as_int(info.get("tvdbId")),
        seasons=tuple(seasons),
        anime=media_type == "tv"
        and any(isinstance(k, dict) and as_int(k.get("id")) == ANIME_KEYWORD for k in keywords),
        genres=tuple(str(g["name"]) for g in raw.get("genres") or [] if isinstance(g, dict) and g.get("name")),
        runtime=runtime or None,
        plex_url=http_url(info.get("plexUrl")) or http_url(info.get("mediaUrl")),
    )


def title_details(config: SeerrConfig, media_type: str, tmdb_id: int) -> Title:
    return parse_title(media_type, seerr_get(config.url, config.apikey, f"{media_type}/{tmdb_id}", timeout=TIMEOUT))


# ---------------------------------------------------------------------------
# seerr's radarr/sonarr servers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Folder:
    path: str
    free_bytes: int | None = None


@dataclass(frozen=True)
class Server:
    id: int
    name: str
    is_4k: bool
    is_default: bool
    profiles: tuple[tuple[int, str], ...] = ()
    root_folders: tuple[Folder, ...] = ()
    tags: tuple[tuple[int, str], ...] = ()
    default_profile: int | None = None
    default_root: str | None = None
    default_tags: tuple[int, ...] = ()
    # sonarr's anime defaults
    anime_profile: int | None = None
    anime_root: str | None = None
    anime_tags: tuple[int, ...] = ()
    # why its profiles, folders and tags couldn't be read (seerr asks the arr live): it
    # can still be requested with seerr's defaults
    problem: str | None = None

    def defaults(self, anime: bool) -> tuple[int | None, str | None, tuple[int, ...]]:
        """seerr's profile, root folder and tags for a request to this server"""
        if anime:
            return (
                self.anime_profile or self.default_profile,
                self.anime_root or self.default_root,
                self.anime_tags or self.default_tags,
            )
        return self.default_profile, self.default_root, self.default_tags

    @property
    def label(self) -> str:
        return f"{self.name} (4K)" if self.is_4k and "4k" not in self.name.casefold() else self.name

    def as_dict(self, anime: bool = False) -> dict[str, Any]:
        profile, root, tags = self.defaults(anime)
        return {
            "id": self.id,
            "name": self.name,
            "is_4k": self.is_4k,
            "is_default": self.is_default,
            "profiles": [{"id": i, "name": n} for i, n in self.profiles],
            "root_folders": [{"path": f.path, "free_bytes": f.free_bytes} for f in self.root_folders],
            "tags": [{"id": i, "label": n} for i, n in self.tags],
            "defaults": {"profile": profile, "root_folder": root, "tags": list(tags)},
            "problem": self.problem,
        }


def _ids(values: object) -> tuple[int, ...]:
    return tuple(i for v in values or [] if (i := as_int(v)) is not None) if isinstance(values, list) else ()


def parse_server(summary: Mapping[str, Any], details: Mapping[str, Any] | None, problem: str | None = None) -> Server | None:
    """a server of seerr's service/<arr> list, with its service/<arr>/<id> details"""
    details = details or {}
    # the details' own copy is the full one: the list gives sonarr's tags as [] and no anime tags
    if isinstance(details.get("server"), dict):
        summary = {**summary, **details["server"]}
    server_id = as_int(summary.get("id"))
    if server_id is None:
        return None
    return Server(
        id=server_id,
        name=str(summary.get("name") or f"Server {server_id}"),
        is_4k=bool(summary.get("is4k")),
        is_default=bool(summary.get("isDefault")),
        profiles=tuple(
            (i, str(p.get("name") or i))
            for p in details.get("profiles") or []
            if isinstance(p, dict) and (i := as_int(p.get("id"))) is not None
        ),
        root_folders=tuple(
            Folder(str(f["path"]), as_int(f.get("freeSpace")))
            for f in details.get("rootFolders") or []
            if isinstance(f, dict) and f.get("path")
        ),
        tags=tuple(
            sorted(
                (
                    (i, str(t.get("label") or i))
                    for t in details.get("tags") or []
                    if isinstance(t, dict) and (i := as_int(t.get("id"))) is not None
                ),
                key=lambda t: t[1].casefold(),
            )
        ),
        default_profile=as_int(summary.get("activeProfileId")),
        default_root=summary.get("activeDirectory") or None,
        default_tags=_ids(summary.get("activeTags")),
        anime_profile=as_int(summary.get("activeAnimeProfileId")),
        anime_root=summary.get("activeAnimeDirectory") or None,
        anime_tags=_ids(summary.get("activeAnimeTags")),
        problem=problem,
    )


def servers(config: SeerrConfig, media_type: str, failure: Any = str) -> list[Server]:
    """seerr's servers for a type, each with its options (read at once: seerr asks each
    arr). failure turns an exception into the text a server's problem shows"""
    arr = ARRS[media_type]
    listed = [s for s in seerr_get(config.url, config.apikey, f"service/{arr}", timeout=TIMEOUT) or [] if isinstance(s, dict)]

    def one(summary: dict[str, Any]) -> Server | None:
        try:
            details = seerr_get(config.url, config.apikey, f"service/{arr}/{as_int(summary.get('id'))}", timeout=TIMEOUT)
        except Exception as e:
            logger.warning("WEB\t Seerr couldn't read %s's options: %s", summary.get("name"), failure(e))
            return parse_server(summary, None, f"Seerr couldn't read its options: {failure(e)}")
        return parse_server(summary, details if isinstance(details, dict) else None)

    if not listed:
        return []
    with ThreadPoolExecutor(max_workers=min(len(listed), 4)) as pool:
        found = list(pool.map(one, listed))
    return [s for s in found if s is not None]


def default_server(found: Iterable[Server], title: Title) -> Server | None:
    """the server a request goes to unless another is picked: seerr's default non-4K one
    while it can be requested there, else the first that can take it"""
    usable = [s for s in found if title.requestable(s.is_4k)]
    for wanted in (
        lambda s: s.is_default and not s.is_4k,
        lambda s: not s.is_4k,
        lambda s: s.is_default,
        lambda s: True,
    ):
        for server in usable:
            if wanted(server):
                return server
    return None


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class User:
    id: int
    name: str
    # what tells two of the same name apart: their plex username or email
    detail: str | None = None


def parse_user(raw: Mapping[str, Any]) -> User | None:
    user_id = as_int(raw.get("id"))
    if user_id is None:
        return None
    name = raw.get("displayName") or raw.get("plexUsername") or raw.get("username") or raw.get("email") or f"user {user_id}"
    detail = raw.get("plexUsername") or raw.get("email") or None
    return User(user_id, str(name), str(detail) if detail and detail != name else None)


def users(config: SeerrConfig) -> tuple[list[User], int | None]:
    """seerr's users by name, and the one the API key requests as"""
    found: list[User] = []
    skip = 0
    while True:
        body = seerr_get(config.url, config.apikey, "user", timeout=TIMEOUT, take=USERS_PAGE, skip=skip)
        page = body.get("results", []) if isinstance(body, dict) else []
        found.extend(u for raw in page if isinstance(raw, dict) and (u := parse_user(raw)))
        if len(page) < USERS_PAGE:
            break
        skip += USERS_PAGE
    me = seerr_get(config.url, config.apikey, "auth/me", timeout=TIMEOUT)
    me_id = as_int(me.get("id")) if isinstance(me, dict) else None
    return sorted(found, key=lambda u: u.name.casefold()), me_id


# ---------------------------------------------------------------------------
# the request
# ---------------------------------------------------------------------------


@dataclass
class Choices:
    """what was picked; None is seerr's default"""

    server: int | None = None
    profile: int | None = None
    root_folder: str | None = None
    tags: tuple[int, ...] | None = None
    seasons: tuple[int, ...] | None = None
    user: int | None = None


@dataclass(frozen=True)
class Planned:
    """a request ready to send, and what it says in words"""

    body: dict[str, Any]
    server: Server
    seasons: tuple[int, ...] = ()
    user: User | None = None


def plan_request(title: Title, found: list[Server], known_users: list[User], choices: Choices) -> Planned:
    """the body of seerr's POST request for these choices, checked against what seerr
    offers. Raises RequestInvalid"""
    arr = ARR_LABELS[ARRS[title.media_type]]
    if not found:
        raise RequestInvalid(f"Seerr has no {arr} server set up, so it couldn't send the request anywhere")
    if choices.server is None:
        server = default_server(found, title)
        if server is None:
            raise RequestInvalid("Everything here is in Plex or requested already")
    else:
        server = next((s for s in found if s.id == choices.server), None)
        if server is None:
            raise RequestInvalid(f"Seerr has no {arr} server {choices.server}")
        if not title.requestable(server.is_4k):
            copy = "4K copy" if server.is_4k else "copy"
            raise RequestInvalid(f"This {copy} is in Plex or requested already")

    body: dict[str, Any] = {
        "mediaType": title.media_type,
        "mediaId": title.tmdb_id,
        "is4k": server.is_4k,
        "serverId": server.id,
    }
    profile, root, tags = server.defaults(title.anime)
    if server.problem:
        if (choices.profile, choices.root_folder, choices.tags) != (None, None, None):
            raise RequestInvalid(f"{server.label}'s options couldn't be read, so only Seerr's defaults can be used")
    else:
        if choices.profile is not None:
            if choices.profile not in {i for i, _ in server.profiles}:
                raise RequestInvalid(f"{server.label} has no quality profile {choices.profile}")
            profile = choices.profile
        if choices.root_folder is not None:
            if choices.root_folder not in {f.path for f in server.root_folders}:
                raise RequestInvalid(f"{choices.root_folder} isn't a root folder of {server.label}")
            root = choices.root_folder
        if choices.tags is not None:
            unknown = set(choices.tags) - {i for i, _ in server.tags}
            if unknown:
                raise RequestInvalid(f"{server.label} has no tag {min(unknown)}")
            tags = tuple(sorted(set(choices.tags)))
        if profile is not None:
            body["profileId"] = profile
        if root:
            body["rootFolder"] = root
        body["tags"] = list(tags)

    seasons: tuple[int, ...] = ()
    if title.media_type == "tv":
        open_now = title.open_seasons(server.is_4k)
        if choices.seasons is None:
            seasons = tuple(title.default_seasons(server.is_4k))
        else:
            closed = sorted(set(choices.seasons) - set(open_now))
            if closed:
                raise RequestInvalid(
                    f"Season {closed[0]} is in Plex or requested already" if len(closed) == 1
                    else f"Seasons {', '.join(map(str, closed))} are in Plex or requested already"
                )
            seasons = tuple(sorted(set(choices.seasons)))
        if not seasons:
            raise RequestInvalid("Pick at least one season")
        body["seasons"] = list(seasons)
        if title.tvdb_id is not None:
            body["tvdbId"] = title.tvdb_id

    user = None
    if choices.user is not None:
        user = next((u for u in known_users if u.id == choices.user), None)
        if user is None:
            raise RequestInvalid(f"Seerr has no user {choices.user}")
        body["userId"] = user.id
    return Planned(body, server, seasons, user)


def send(config: SeerrConfig, planned: Planned) -> dict[str, Any]:
    """the request seerr made (its id and status: 1 pending, 2 approved)"""
    answer = seerr_post(config.url, config.apikey, "request", planned.body, timeout=TIMEOUT)
    return answer if isinstance(answer, dict) else {}


def seerr_link(config: SeerrConfig, media_type: str, tmdb_id: int) -> str:
    return f"{(config.public_url or config.url).rstrip('/')}/{media_type}/{tmdb_id}"
