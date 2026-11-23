"""assistant_tools.py - what the Assistant (web.assistant) can look at: the services'
read-only endpoints, helperr's own database and log, and the Status page's check.

Every tool only reads. Paths are allowlisted per service: nothing that changes state
(a GET of /library/sections/1/refresh scans in Plex, radarr's GET release searches the
indexers) and nothing that holds other services' secrets (the arrs' indexer, download
client and notification settings). Fixes go through propose_action
(web.assistant_actions), which only proposes them.

What a tool returns goes to Anthropic, so it's cleaned first (clean()): the config's
secrets redacted, every url cut to its host (an arr's history holds indexer download
urls with the indexer's key, which isn't ours to redact), noisy fields dropped and the
text capped at MAX_RESULT_CHARS"""

import json
import logging
import re
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from config.models import ConfigSettings
from logging_config import redacted, url_hosts
from tautulli import tautulli_get
from web import logs
from web.db import Base
from web.seerr import seerr_get
from web.status import check_all, failure

logger = logging.getLogger(__name__)

TIMEOUT = 30
# a tool's answer is cut here, saying so: a whole library would fill the context
MAX_RESULT_CHARS = 24_000
# rows a database query returns, and characters of one value
MAX_ROWS = 200
MAX_CELL_CHARS = 2_000
# seconds a database query may take
QUERY_SECONDS = 5
# items of a plex list, unless asked for more
PLEX_PAGE = 50
# log events returned
MAX_EVENTS = 100

# fields that say nothing to an investigation and take room
DROP_KEYS = {
    "images", "remotePoster", "ratings", "alternateTitles", "addOptions", "overview",
    "Image", "Genre", "Country", "Role", "Director", "Writer", "Producer", "Similar",
    "Rating", "UltraBlurColors", "thumb", "art", "banner", "theme", "posterUrl",
    "backdropPath", "posterPath", "credits", "relatedVideos", "watchProviders", "keywords",
}

# read-only paths under each arr's /api/v3. Nothing with other services' secrets
# (indexer, downloadclient, notification, importlist, config), nothing that acts
# (release searches the indexers, manualimport reads the disk)
ARR_PATHS: dict[str, tuple[str, ...]] = {
    "sonarr": (
        r"series", r"series/\d+", r"series/lookup", r"episode", r"episode/\d+", r"episodefile",
        r"episodefile/\d+", r"history", r"history/series", r"history/since", r"queue",
        r"queue/details", r"queue/status", r"wanted/missing", r"wanted/cutoff", r"calendar",
        r"health", r"system/status", r"system/task", r"rootfolder", r"diskspace",
        r"qualityprofile", r"qualityprofile/\d+", r"qualitydefinition", r"customformat", r"tag",
        r"tag/detail", r"blocklist", r"log", r"command", r"command/\d+", r"parse", r"rename",
        r"languageprofile",
    ),
    "radarr": (
        r"movie", r"movie/\d+", r"movie/lookup", r"moviefile", r"moviefile/\d+", r"history",
        r"history/movie", r"history/since", r"queue", r"queue/details", r"queue/status",
        r"wanted/missing", r"wanted/cutoff", r"calendar", r"health", r"system/status",
        r"system/task", r"rootfolder", r"diskspace", r"qualityprofile", r"qualityprofile/\d+",
        r"qualitydefinition", r"customformat", r"tag", r"tag/detail", r"blocklist", r"log",
        r"command", r"command/\d+", r"parse", r"rename", r"collection", r"credit",
    ),
}
# plex: lists and items, never a refresh or scan (both are GETs)
PLEX_PATHS = (
    r"library/sections", r"library/sections/\d+/all", r"library/sections/\d+/search",
    r"library/sections/\d+/recentlyAdded", r"library/metadata/\d+", r"library/metadata/\d+/children",
    r"library/metadata/\d+/allLeaves", r"hubs/search", r"status/sessions",
    r"status/sessions/history/all", r"transcode/sessions", r"activities", r"library/recentlyAdded",
)
# tautulli's commands that read
TAUTULLI_COMMANDS = (
    "get_history", "get_activity", "get_metadata", "get_stream_data", "get_item_watch_time_stats",
    "get_item_user_stats", "get_libraries", "get_library_media_info", "get_users", "get_user",
    "search", "server_status", "get_recently_added", "get_children_metadata",
)
SABNZBD_MODES = ("queue", "history", "warnings", "status")
BAZARR_PATHS = (
    r"api/episodes", r"api/movies", r"api/series", r"api/episodes/history", r"api/movies/history",
    r"api/episodes/wanted", r"api/movies/wanted", r"api/system/health", r"api/system/status",
    r"api/providers", r"api/badges",
)
SEERR_PATHS = (
    r"request", r"request/\d+", r"request/count", r"issue", r"issue/\d+", r"issue/count",
    r"media", r"media/\d+", r"movie/\d+", r"tv/\d+", r"tv/\d+/season/\d+", r"search", r"user",
    r"user/\d+", r"user/\d+/requests", r"status",
)

# helperr's tables the database tool may read. Never app_settings (the webhook key, the
# login's hash), config_secrets or login_sessions; the assistant's own aren't useful
DB_TABLES = (
    "plays", "playbacks", "media_requests", "library_items", "health_incidents", "job_runs",
    "schedules", "trash_events", "kometa_runs", "config_versions", "remover_rule_settings",
)


class ToolError(Exception):
    """the tool couldn't do what was asked; its text goes back to Claude, safe to show"""


@dataclass
class ToolContext:
    """what a question's tools run with"""

    settings: ConfigSettings
    database: Path
    now: datetime  # naive utc


# ---------------------------------------------------------------------------
# cleaning what goes out
# ---------------------------------------------------------------------------


def _trimmed(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _trimmed(v) for k, v in value.items() if k not in DROP_KEYS}
    if isinstance(value, list):
        return [_trimmed(v) for v in value]
    return value


def clean(value: Any) -> str:
    """a tool's answer as compact json (or text), safe to send: secrets redacted, urls cut
    to their host, capped at MAX_RESULT_CHARS"""
    text = value if isinstance(value, str) else json.dumps(_trimmed(value), separators=(",", ":"), default=str)
    text = redacted(url_hosts(text))
    if len(text) > MAX_RESULT_CHARS:
        text = (
            text[:MAX_RESULT_CHARS]
            + f"\n[cut: {len(text):,} characters in all, {MAX_RESULT_CHARS:,} shown. Ask for less: filter, page or pick an id]"
        )
    return text


def _allowed(path: str, patterns: tuple[str, ...], label: str) -> str:
    path = path.strip().strip("/")
    if not any(re.fullmatch(p, path) for p in patterns):
        raise ToolError(f"{label} path '{path}' isn't one this tool may read")
    return path


def _params(params: object) -> dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ToolError("params must be an object of query parameters")
    for key, value in params.items():
        values = value if isinstance(value, list) else [value]
        if not all(isinstance(v, (str, int, float, bool)) for v in values):
            raise ToolError(f"query parameter '{key}' must be text, a number, true/false or a list of them")
    # requests would send True as "True"
    return {
        k: [str(x).lower() if isinstance(x, bool) else x for x in v]
        if isinstance(v, list)
        else (str(v).lower() if isinstance(v, bool) else v)
        for k, v in params.items()
    }


def _json(response: requests.Response, label: str) -> Any:
    if response.status_code in (401, 403):
        raise ToolError(f"{label} refused helperr's key (HTTP {response.status_code})")
    if response.status_code == 404:
        raise ToolError(f"{label} has no such thing (HTTP 404)")
    if response.status_code >= 400:
        raise ToolError(f"{label} answered HTTP {response.status_code}: {response.text[:500]}")
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError:
        raise ToolError(f"{label} answered with a web page, not its API") from None


def _connected(service: object, label: str) -> Any:
    if service is None:
        raise ToolError(f"{label} isn't connected in helperr (Settings → Connections)")
    return service


# ---------------------------------------------------------------------------
# the tools
# ---------------------------------------------------------------------------


def service_status(ctx: ToolContext, **_: Any) -> Any:
    """the Status page's check, now"""
    statuses = check_all(ctx.settings, ctx.now)
    answer = []
    for s in statuses:
        if s.state == "off":
            answer.append({"service": s.service, "state": "not connected"})
            continue
        answer.append({
            "service": s.service,
            "state": s.state,
            "version": s.version,
            "error": s.error,
            "problems": [f"{p.level}: {p.text}" for p in s.problems],
            "facts": dict(s.facts),
            "stuck": [{"title": i.title, "detail": i.detail, "download_id": i.key} for i in s.items],
            "streams": [
                {"user": st.user, "title": st.title, "decision": st.decision, "player": st.player,
                 "cause": st.cause, "now": st.now, "change": st.change}
                for st in s.streams
            ],
            "downloads": [
                {"title": d.title, "service": d.service, "state": d.state, "progress": d.progress,
                 "release": d.release, "download_id": d.key}
                for d in s.downloads
            ],
        })
    return answer


def find_media(ctx: ToolContext, query: str = "", service: str = "", **_: Any) -> Any:
    """series/movies whose title holds the query, from sonarr/radarr"""
    words = str(query).casefold().split()
    if not words:
        raise ToolError("query is empty")
    services = [service] if service else ["sonarr", "radarr"]
    found: list[dict[str, Any]] = []
    for name in services:
        if name not in ARR_PATHS:
            raise ToolError("service must be sonarr or radarr")
        arr = getattr(ctx.settings, name)
        if arr is None or not arr.baseurl or not arr.apikey:
            if service:
                raise ToolError(f"{name.title()} isn't connected in helperr")
            continue
        path = "series" if name == "sonarr" else "movie"
        response = requests.get(
            f"{arr.baseurl.rstrip('/')}/api/v3/{path}", headers={"X-Api-Key": arr.apikey}, timeout=TIMEOUT
        )
        for media in _json(response, name.title()):
            titles = [str(media.get("title") or "")] + [
                str(t.get("title") or "") for t in media.get("alternateTitles") or [] if isinstance(t, dict)
            ]
            if not any(all(w in t.casefold() for w in words) for t in titles):
                continue
            stats = media.get("statistics") or {}
            found.append({
                "service": name,
                "id": media.get("id"),
                "title": media.get("title"),
                "year": media.get("year"),
                "path": media.get("path"),
                "monitored": media.get("monitored"),
                "status": media.get("status"),
                "tvdbId": media.get("tvdbId"),
                "tmdbId": media.get("tmdbId"),
                "imdbId": media.get("imdbId"),
                "qualityProfileId": media.get("qualityProfileId"),
                "tags": media.get("tags"),
                "added": media.get("added"),
                "hasFile": media.get("hasFile"),
                "episodes": (
                    f"{stats.get('episodeFileCount')} files of {stats.get('episodeCount')} aired"
                    if name == "sonarr"
                    else None
                ),
            })
    return found[:50] if found else f"Nothing titled like '{query}'"


def arr_get(ctx: ToolContext, service: str = "", path: str = "", params: object = None, **_: Any) -> Any:
    if service not in ARR_PATHS:
        raise ToolError("service must be sonarr or radarr")
    arr = _connected(getattr(ctx.settings, service), service.title())
    path = _allowed(path, ARR_PATHS[service], service.title())
    response = requests.get(
        f"{arr.baseurl.rstrip('/')}/api/v3/{path}",
        headers={"X-Api-Key": arr.apikey},
        params=_params(params),
        timeout=TIMEOUT,
    )
    return _json(response, service.title())


def plex_get(ctx: ToolContext, path: str = "", params: object = None, **_: Any) -> Any:
    plex = ctx.settings.plex
    if plex is None or not plex.baseurl or not plex.token:
        raise ToolError("Plex isn't connected in helperr (Settings → Connections)")
    path = _allowed(path, PLEX_PATHS, "Plex")
    query = _params(params)
    query.setdefault("X-Plex-Container-Start", 0)
    query.setdefault("X-Plex-Container-Size", PLEX_PAGE)
    response = requests.get(
        f"{plex.baseurl.rstrip('/')}/{path}",
        headers={"X-Plex-Token": plex.token, "Accept": "application/json"},
        params=query,
        timeout=TIMEOUT,
    )
    return _json(response, "Plex")


def tautulli(ctx: ToolContext, cmd: str = "", params: object = None, **_: Any) -> Any:
    t = _connected(ctx.settings.tautulli, "Tautulli")
    if cmd not in TAUTULLI_COMMANDS:
        raise ToolError(f"Tautulli command '{cmd}' isn't one this tool may run")
    query = _params(params)
    query.pop("apikey", None)
    query.pop("cmd", None)
    return tautulli_get(t.url, t.apikey, cmd, timeout=TIMEOUT, **query)


def sabnzbd(ctx: ToolContext, mode: str = "", params: object = None, **_: Any) -> Any:
    sab = _connected(ctx.settings.sabnzbd, "SABnzbd")
    if mode not in SABNZBD_MODES:
        raise ToolError(f"mode must be one of {', '.join(SABNZBD_MODES)}")
    query = {k: v for k, v in _params(params).items() if k not in ("apikey", "mode", "output", "name")}
    query.setdefault("limit", 50)
    response = requests.get(
        f"{sab.url.rstrip('/')}/api",
        params={**query, "mode": mode, "output": "json", "apikey": sab.apikey},
        timeout=TIMEOUT,
    )
    body = _json(response, "SABnzbd")
    if isinstance(body, dict) and body.get("status") is False:
        raise ToolError(f"SABnzbd refused: {body.get('error') or 'unknown error'}")
    return body


def bazarr_get(ctx: ToolContext, path: str = "", params: object = None, **_: Any) -> Any:
    bazarr = _connected(ctx.settings.bazarr, "Bazarr")
    path = _allowed(path, BAZARR_PATHS, "Bazarr")
    response = requests.get(
        f"{bazarr.url.rstrip('/')}/{path}",
        headers={"X-API-KEY": bazarr.apikey},
        params=_params(params),
        timeout=TIMEOUT,
    )
    return _json(response, "Bazarr")


def seerr(ctx: ToolContext, path: str = "", params: object = None, **_: Any) -> Any:
    s = _connected(ctx.settings.seerr, "Seerr")
    path = _allowed(path, SEERR_PATHS, "Seerr")
    return seerr_get(s.url, s.apikey, path, timeout=TIMEOUT, **_params(params))


def helperr_events(ctx: ToolContext, level: str = "info", contains: str = "", limit: int = 50, **_: Any) -> Any:
    """the web app's log (Activity → Events), newest first"""
    if level not in logs.LEVELS:
        raise ToolError(f"level must be one of {', '.join(logs.LEVELS)}")
    try:
        limit = max(1, min(int(limit), MAX_EVENTS))
    except TypeError, ValueError:
        raise ToolError("limit must be a number") from None
    needle = str(contains or "").casefold()
    filters = logs.EventFilters(level=level)
    found = []
    for event in logs.read_events():
        if not filters.keep(event):
            continue
        if needle and needle not in f"{event.component} {event.message} {' '.join(event.details)}".casefold():
            continue
        found.append({
            "time": event.time.isoformat(timespec="seconds") + "Z",
            "level": event.level,
            "component": event.component,
            "message": event.message,
            "details": "\n".join(event.details)[:MAX_CELL_CHARS],
        })
        if len(found) >= limit:
            break
    return found or "No events match"


def _table_schema() -> str:
    lines = []
    for name in DB_TABLES:
        table = Base.metadata.tables[name]
        lines.append(f"{name}({', '.join(c.name for c in table.columns)})")
    return "\n".join(lines)


def _authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _trigger: str | None) -> int:
    """sqlite's check of each step of a statement: selecting and reading the allowed
    tables, and functions (json_extract...), nothing else"""
    if action == sqlite3.SQLITE_READ:
        return sqlite3.SQLITE_OK if arg1 in DB_TABLES else sqlite3.SQLITE_DENY
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def helperr_query(ctx: ToolContext, sql: str = "", **_: Any) -> Any:
    """one read-only SELECT on helperr's database"""
    sql = str(sql).strip().rstrip(";")
    if not sql:
        raise ToolError("sql is empty")
    # read only whatever the authorizer allows: a write fails at the file
    connection = sqlite3.connect(f"{Path(ctx.database).resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        connection.set_authorizer(_authorizer)
        deadline = time.monotonic() + QUERY_SECONDS
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        try:
            cursor = connection.execute(sql)
            columns = [d[0] for d in cursor.description or []]
            rows = cursor.fetchmany(MAX_ROWS + 1)
        except sqlite3.Error as e:
            raise ToolError(f"The query failed: {e}") from None
    finally:
        connection.close()

    def cell(value: Any) -> Any:
        if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
            return value[:MAX_CELL_CHARS] + "…"
        return value

    answer: dict[str, Any] = {"columns": columns, "rows": [[cell(v) for v in row] for row in rows[:MAX_ROWS]]}
    if len(rows) > MAX_ROWS:
        answer["note"] = f"only the first {MAX_ROWS} rows"
    return answer


# ---------------------------------------------------------------------------
# definitions sent to the api: kept in one fixed order, so the prompt cache holds
# ---------------------------------------------------------------------------

PARAMS_SCHEMA = {
    "type": "object",
    "description": "Query parameters, e.g. {\"seriesId\": 12}",
    "additionalProperties": {
        "anyOf": [
            {"type": ["string", "integer", "number", "boolean"]},
            {"type": "array", "items": {"type": ["string", "integer", "number", "boolean"]}},
        ]
    },
}


def _paths(patterns: tuple[str, ...]) -> str:
    return ", ".join(p.replace(r"\d+", "{id}") for p in patterns)


DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "service_status",
        "description": (
            "Helperr's Status page, checked now: each service's state (ok/warn/bad), problems, "
            "stuck downloads, Plex streams and downloads in progress. Start here for 'is something broken'."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "find_media",
        "description": (
            "Find series (Sonarr) or movies (Radarr) by title words. Returns their ids (for the other "
            "tools), path, monitored, tvdb/tmdb/imdb ids and file counts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "title words, e.g. 'office us'"},
                "service": {"type": "string", "enum": ["sonarr", "radarr"], "description": "leave out for both"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "arr_get",
        "description": (
            "GET from Sonarr's or Radarr's /api/v3 (read only). Sonarr paths: "
            + _paths(ARR_PATHS["sonarr"])
            + ". Radarr paths: "
            + _paths(ARR_PATHS["radarr"])
            + ". Useful: Sonarr episode?seriesId=&seasonNumber= (episodes with hasFile, episodeFileId), "
            "episodefile?seriesId= (files: path, quality, mediaInfo, dateAdded), history/series?seriesId=&"
            "episodeId= (grabbed, imported, deleted, failed, renamed, with reasons), queue?includeEpisode=true, "
            "log?page=1&pageSize=50&level=warn. Radarr: moviefile?movieId=, history/movie?movieId=. Lists "
            "are big: filter by id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "enum": ["sonarr", "radarr"]},
                "path": {"type": "string", "description": "after /api/v3/, e.g. episodefile/123"},
                "params": PARAMS_SCHEMA,
            },
            "required": ["service", "path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "plex_get",
        "description": (
            "GET from Plex (read only, json). Paths: " + _paths(PLEX_PATHS) + ". hubs/search?query= finds "
            "items; library/metadata/{ratingKey} gives an item's Media (versions) and Part (files, sizes); "
            "allLeaves lists a show's episodes. Lists return "
            f"{PLEX_PAGE} items unless X-Plex-Container-Size says more. Plex sees files under different "
            "paths than Sonarr/Radarr."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "e.g. library/metadata/12345"},
                "params": PARAMS_SCHEMA,
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "tautulli",
        "description": (
            "Run a read-only Tautulli command: " + ", ".join(TAUTULLI_COMMANDS) + ". get_history (params "
            "rating_key, grandparent_rating_key, user_id, search, length) has every play including partial "
            "ones; get_stream_data (row_id = history id) says how a play was streamed and why it transcoded."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "enum": list(TAUTULLI_COMMANDS)},
                "params": PARAMS_SCHEMA,
            },
            "required": ["cmd"],
            "additionalProperties": False,
        },
    },
    {
        "name": "sabnzbd",
        "description": (
            "Read SABnzbd: queue, history (params search, limit, failed_only=1: failed downloads with "
            "fail_message), warnings, status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": list(SABNZBD_MODES)},
                "params": PARAMS_SCHEMA,
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bazarr_get",
        "description": (
            "GET from Bazarr (subtitles, read only). Paths: " + _paths(BAZARR_PATHS) + ". e.g. "
            "api/episodes?seriesid[]=<sonarr series id> for an episode's subtitles and what's missing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "params": PARAMS_SCHEMA},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "seerr",
        "description": (
            "GET from Seerr/Overseerr's /api/v1 (read only). Paths: " + _paths(SEERR_PATHS) + ". issue "
            "lists problems users reported (params take, skip, filter=open), with their comments; request "
            "lists requests."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "params": PARAMS_SCHEMA},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "helperr_events",
        "description": (
            "Helperr's own log (Activity → Events), newest first: its jobs, syncs, health checks and "
            "errors. level is the lowest shown."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": list(logs.LEVELS)},
                "contains": {"type": "string", "description": "only events containing this text"},
                "limit": {"type": "integer", "description": f"up to {MAX_EVENTS}"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "helperr_query",
        "description": (
            "One read-only SQLite SELECT on helperr's database (times are naive UTC; json columns work with "
            f"json_extract). At most {MAX_ROWS} rows. Tables:\n"
            + _table_schema()
            + "\nplays: Tautulli's history copied (guid plex://..., season/episode, transcode_decision). "
            "playbacks: recent plays with stream details and why they transcoded. job_runs: helperr's runs "
            "(movers, removers, empty_trash...) with result json (changes, removals, errors). "
            "health_incidents: Status problems over time. library_items: Plex items with plays. "
            "media_requests: Seerr requests and whether the requester watched."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
]

TOOLS: dict[str, Callable[..., Any]] = {
    "service_status": service_status,
    "find_media": find_media,
    "arr_get": arr_get,
    "plex_get": plex_get,
    "tautulli": tautulli,
    "sabnzbd": sabnzbd,
    "bazarr_get": bazarr_get,
    "seerr": seerr,
    "helperr_events": helperr_events,
    "helperr_query": helperr_query,
}


def secrets_of(settings: ConfigSettings) -> list[str]:
    return [
        s
        for s in (
            getattr(settings.radarr, "apikey", None), getattr(settings.sonarr, "apikey", None),
            getattr(settings.plex, "token", None), getattr(settings.tautulli, "apikey", None),
            getattr(settings.sabnzbd, "apikey", None), getattr(settings.bazarr, "apikey", None),
            getattr(settings.seerr, "apikey", None), getattr(settings.unraid, "apikey", None),
            getattr(settings.assistant, "apikey", None), getattr(settings.plex_host, "password", None),
        )
        if s
    ]


def run_tool(ctx: ToolContext, name: str, arguments: object) -> tuple[str, bool]:
    """a tool's cleaned answer, and whether it's an error"""
    tool = TOOLS.get(name)
    if tool is None:
        return f"There's no tool named {name}", True
    if not isinstance(arguments, dict):
        return "The tool's input must be an object", True
    try:
        return clean(tool(ctx, **arguments)), False
    except ToolError as e:
        return clean(str(e)), True
    except Exception as e:
        return clean(failure(e, f"Assistant tool {name}", *secrets_of(ctx.settings))), True


def run_tools(ctx: ToolContext, calls: list[tuple[str, str, object]]) -> list[tuple[str, str, bool]]:
    """(id, answer, is error) of each (id, name, arguments), run side by side: each is a
    request to a service on the lan"""
    if not calls:
        return []
    with ThreadPoolExecutor(max_workers=min(len(calls), 6)) as pool:
        futures = [(call_id, pool.submit(run_tool, ctx, name, args)) for call_id, name, args in calls]
        return [(call_id, *future.result()) for call_id, future in futures]
