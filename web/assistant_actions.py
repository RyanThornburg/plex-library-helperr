"""assistant_actions.py - fixes the Assistant can propose, which only happen when someone
confirms them on the chat's page.

Claude proposes one with propose_action; helperr checks its parameters and writes what
it does (describe()) from the services' own data, so the button never says what Claude
claims, only what will be sent. Nothing here deletes library media: a search, a refresh,
or removing a download from a queue (the same as the Status page's queue page, which
isn't behind REMOVERS_ALLOW_APPLY either)"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

from config.models import ArrConfig, ConfigSettings
from web.status import BLOCKLIST, REMOVAL_METHODS, ArrQueue

logger = logging.getLogger(__name__)

TIMEOUT = 30
# episodes one search may name
MAX_EPISODES = 50


class ActionInvalid(Exception):
    """the proposal can't be made or done as asked: its text is safe to show"""


@dataclass
class Plan:
    """a checked proposal: what to send, and what it does in words"""

    params: dict[str, Any]
    description: str
    # the button's words, naming what happens
    button: str
    # removing a download can't be undone
    danger: bool = False


def _arr(settings: ConfigSettings, service: str) -> ArrConfig:
    arr = getattr(settings, service, None) if service in ("sonarr", "radarr") else None
    if arr is None or not arr.baseurl or not arr.apikey:
        raise ActionInvalid(f"{service.title() or 'That service'} isn't connected in helperr")
    return arr


def _get(arr: ArrConfig, path: str, label: str, **params: Any) -> Any:
    response = requests.get(
        f"{arr.baseurl.rstrip('/')}/api/v3/{path}", headers={"X-Api-Key": arr.apikey},
        params=params or None, timeout=TIMEOUT,
    )
    if response.status_code == 404:
        raise ActionInvalid(f"{label} has no {path}")
    response.raise_for_status()
    return response.json()


def _command(arr: ArrConfig, body: dict[str, Any], label: str) -> str:
    response = requests.post(
        f"{arr.baseurl.rstrip('/')}/api/v3/command", headers={"X-Api-Key": arr.apikey}, json=body, timeout=TIMEOUT
    )
    response.raise_for_status()
    answer = response.json() if response.content else {}
    return f"{label} started {body['name']} (command {answer.get('id', '?')}, {answer.get('status', 'queued')})"


def _int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ActionInvalid(f"{name} must be a whole number")
    return value


def _series(arr: ArrConfig, series_id: int) -> dict[str, Any]:
    return _get(arr, f"series/{series_id}", "Sonarr")


def _episode_label(series: dict[str, Any], episode: dict[str, Any]) -> str:
    return (
        f"{series.get('title')} S{episode.get('seasonNumber', 0):02d}E{episode.get('episodeNumber', 0):02d}"
        f" “{episode.get('title') or 'TBA'}”"
    )


# ---------------------------------------------------------------------------
# each action: check(settings, proposal) -> Plan, do(settings, params) -> what happened
# ---------------------------------------------------------------------------


def check_episode_search(settings: ConfigSettings, p: dict[str, Any]) -> Plan:
    arr = _arr(settings, "sonarr")
    ids = p.get("episode_ids")
    if not isinstance(ids, list) or not ids or len(ids) > MAX_EPISODES:
        raise ActionInvalid(f"episode_ids must list 1 to {MAX_EPISODES} episode ids")
    ids = list(dict.fromkeys(_int(i, "an episode id") for i in ids))
    episodes = [_get(arr, f"episode/{i}", "Sonarr") for i in ids]
    series = _series(arr, int(episodes[0].get("seriesId")))
    labels = [_episode_label(series, e) for e in episodes]
    n = len(ids)
    return Plan(
        {"episode_ids": ids},
        f"Sonarr searches its indexers for {'; '.join(labels)} and grabs the best release its quality profile allows.",
        f"Search for {n} episode{'s' if n != 1 else ''}",
    )


def do_episode_search(settings: ConfigSettings, params: dict[str, Any]) -> str:
    return _command(_arr(settings, "sonarr"), {"name": "EpisodeSearch", "episodeIds": params["episode_ids"]}, "Sonarr")


def check_season_search(settings: ConfigSettings, p: dict[str, Any]) -> Plan:
    arr = _arr(settings, "sonarr")
    series_id, season = _int(p.get("media_id"), "media_id"), _int(p.get("season"), "season")
    series = _series(arr, series_id)
    return Plan(
        {"media_id": series_id, "season": season},
        f"Sonarr searches for season {season} of {series.get('title')} (a season pack, else each missing or "
        "upgradable episode).",
        f"Search season {season}",
    )


def do_season_search(settings: ConfigSettings, params: dict[str, Any]) -> str:
    body = {"name": "SeasonSearch", "seriesId": params["media_id"], "seasonNumber": params["season"]}
    return _command(_arr(settings, "sonarr"), body, "Sonarr")


def check_series_refresh(settings: ConfigSettings, p: dict[str, Any]) -> Plan:
    arr = _arr(settings, "sonarr")
    series_id = _int(p.get("media_id"), "media_id")
    series = _series(arr, series_id)
    return Plan(
        {"media_id": series_id},
        f"Sonarr refreshes {series.get('title')}'s information and rescans its folder ({series.get('path')}) "
        "for files it lost track of.",
        "Refresh and rescan",
    )


def do_series_refresh(settings: ConfigSettings, params: dict[str, Any]) -> str:
    return _command(_arr(settings, "sonarr"), {"name": "RefreshSeries", "seriesId": params["media_id"]}, "Sonarr")


def _movie(settings: ConfigSettings, p: dict[str, Any]) -> tuple[ArrConfig, int, dict[str, Any]]:
    arr = _arr(settings, "radarr")
    movie_id = _int(p.get("media_id"), "media_id")
    return arr, movie_id, _get(arr, f"movie/{movie_id}", "Radarr")


def check_movie_search(settings: ConfigSettings, p: dict[str, Any]) -> Plan:
    _, movie_id, movie = _movie(settings, p)
    return Plan(
        {"media_id": movie_id},
        f"Radarr searches its indexers for {movie.get('title')} ({movie.get('year')}) and grabs the best "
        "release its quality profile allows.",
        "Search for the movie",
    )


def do_movie_search(settings: ConfigSettings, params: dict[str, Any]) -> str:
    return _command(_arr(settings, "radarr"), {"name": "MoviesSearch", "movieIds": [params["media_id"]]}, "Radarr")


def check_movie_refresh(settings: ConfigSettings, p: dict[str, Any]) -> Plan:
    _, movie_id, movie = _movie(settings, p)
    return Plan(
        {"media_id": movie_id},
        f"Radarr refreshes {movie.get('title')} ({movie.get('year')})'s information and rescans its folder "
        f"({movie.get('path')}).",
        "Refresh and rescan",
    )


def do_movie_refresh(settings: ConfigSettings, params: dict[str, Any]) -> str:
    return _command(_arr(settings, "radarr"), {"name": "RefreshMovie", "movieIds": [params["media_id"]]}, "Radarr")


def _queue_records(arr: ArrConfig, service: str, download_id: str) -> list[dict[str, Any]]:
    include = {"includeMovie": "true"} if service == "radarr" else {"includeSeries": "true", "includeEpisode": "true"}
    queue = _get(arr, "queue", service.title(), pageSize=2000, **include)
    return [r for r in queue.get("records") or [] if str(r.get("downloadId") or "") == download_id]


def check_remove_download(settings: ConfigSettings, p: dict[str, Any]) -> Plan:
    service = str(p.get("service") or "")
    arr = _arr(settings, service)
    download_id = str(p.get("download_id") or "").strip()
    method = str(p.get("method") or "client")
    blocklist = str(p.get("blocklist") or "search")
    if not download_id:
        raise ActionInvalid("download_id is needed: the queue record's downloadId")
    if method not in REMOVAL_METHODS or blocklist not in BLOCKLIST:
        raise ActionInvalid(f"method must be {' or '.join(REMOVAL_METHODS)}, blocklist {', '.join(BLOCKLIST)}")
    records = _queue_records(arr, service, download_id)
    if not records:
        raise ActionInvalid(f"{service.title()}'s queue has no download {download_id}")
    release = records[0].get("title") or download_id
    return Plan(
        {"service": service, "download_id": download_id, "method": method, "blocklist": blocklist},
        f"{service.title()} removes the download {release} ({len(records)} queue record"
        f"{'s' if len(records) != 1 else ''}) from its queue. {REMOVAL_METHODS[method]}. {BLOCKLIST[blocklist]}.",
        "Remove the download",
        danger=True,
    )


def do_remove_download(settings: ConfigSettings, params: dict[str, Any]) -> str:
    service = params["service"]
    arr = _arr(settings, service)
    # the arr looks every id up first: send the ones it lists now
    records = _queue_records(arr, service, params["download_id"])
    if not records:
        return f"{service.title()}'s queue no longer has it, nothing to remove"
    queue = ArrQueue(service, service.title(), arr)
    queue.remove([r["id"] for r in records if isinstance(r.get("id"), int)], params["method"], params["blocklist"])
    return f"{service.title()} removed the download ({len(records)} queue records)"


def _plex(settings: ConfigSettings) -> tuple[str, str]:
    plex = settings.plex
    if plex is None or not plex.baseurl or not plex.token:
        raise ActionInvalid("Plex isn't connected in helperr")
    return plex.baseurl.rstrip("/"), plex.token


def check_plex_refresh(settings: ConfigSettings, p: dict[str, Any]) -> Plan:
    base, token = _plex(settings)
    key = _int(p.get("rating_key"), "rating_key")
    response = requests.get(
        f"{base}/library/metadata/{key}", headers={"X-Plex-Token": token, "Accept": "application/json"}, timeout=TIMEOUT
    )
    if response.status_code == 404:
        raise ActionInvalid(f"Plex has no item {key}")
    response.raise_for_status()
    items = (response.json().get("MediaContainer") or {}).get("Metadata") or [{}]
    item = items[0]
    title = " – ".join(str(t) for t in (item.get("grandparentTitle"), item.get("parentTitle"), item.get("title")) if t)
    return Plan(
        {"rating_key": key},
        f"Plex refreshes the metadata of {title or key} ({item.get('type', 'item')}) and reads its files again.",
        "Refresh in Plex",
    )


def do_plex_refresh(settings: ConfigSettings, params: dict[str, Any]) -> str:
    base, token = _plex(settings)
    response = requests.put(
        f"{base}/library/metadata/{params['rating_key']}/refresh", headers={"X-Plex-Token": token}, timeout=TIMEOUT
    )
    response.raise_for_status()
    return "Plex started refreshing it"


# name: (its parameters, for Claude; check; do)
ACTIONS: dict[str, tuple[str, Callable[[ConfigSettings, dict[str, Any]], Plan], Callable[[ConfigSettings, dict[str, Any]], str]]] = {
    "sonarr_episode_search": ("episode_ids", check_episode_search, do_episode_search),
    "sonarr_season_search": ("media_id (series id), season", check_season_search, do_season_search),
    "sonarr_series_refresh": ("media_id (series id)", check_series_refresh, do_series_refresh),
    "radarr_movie_search": ("media_id (movie id)", check_movie_search, do_movie_search),
    "radarr_movie_refresh": ("media_id (movie id)", check_movie_refresh, do_movie_refresh),
    "remove_download": ("service, download_id, method, blocklist", check_remove_download, do_remove_download),
    "plex_refresh": ("rating_key", check_plex_refresh, do_plex_refresh),
}

DEFINITION: dict[str, Any] = {
    "name": "propose_action",
    "description": (
        "Propose one fix for the user to confirm. It does NOT run: the user sees a button on the page and decides. "
        "Never say it was done. Actions and their parameters: "
        + "; ".join(f"{name} ({hint})" for name, (hint, _, _) in ACTIONS.items())
        + ". sonarr_series_refresh / radarr_movie_refresh refresh information and rescan the folder for files "
        "the arr lost track of. remove_download takes a queue record's downloadId; method client (delete it in "
        "the download client too) or ignore (queue only); blocklist none, search (blocklist and search for "
        "another release) or only. plex_refresh refreshes an item's metadata and reads its files again. "
        "Propose only what your findings support, one call per fix."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "reason": {"type": "string", "description": "why, in one or two sentences for the user"},
            "episode_ids": {"type": "array", "items": {"type": "integer"}},
            "media_id": {"type": "integer"},
            "season": {"type": "integer"},
            "service": {"type": "string", "enum": ["sonarr", "radarr"]},
            "download_id": {"type": "string"},
            "method": {"type": "string", "enum": list(REMOVAL_METHODS)},
            "blocklist": {"type": "string", "enum": list(BLOCKLIST)},
            "rating_key": {"type": "integer"},
        },
        "required": ["action", "reason"],
        "additionalProperties": False,
    },
}


def check(settings: ConfigSettings, proposal: dict[str, Any]) -> Plan:
    """a proposal checked against the services now. Raises ActionInvalid"""
    action = ACTIONS.get(str(proposal.get("action")))
    if action is None:
        raise ActionInvalid(f"unknown action, use one of {', '.join(ACTIONS)}")
    try:
        return action[1](settings, proposal)
    except ActionInvalid:
        raise
    except (requests.RequestException, ValueError, TypeError, KeyError, AttributeError) as e:
        raise ActionInvalid(f"couldn't check it: {type(e).__name__}") from e


def button(kind: str, params: dict[str, Any]) -> tuple[str, bool]:
    """the confirm button's words and whether it's the danger button, for a stored
    action. Rebuilt from its kind, not stored"""
    words = {
        "sonarr_episode_search": f"Search for {len(params.get('episode_ids') or [])} episode"
        + ("s" if len(params.get("episode_ids") or []) != 1 else ""),
        "sonarr_season_search": f"Search season {params.get('season')}",
        "sonarr_series_refresh": "Refresh and rescan",
        "radarr_movie_search": "Search for the movie",
        "radarr_movie_refresh": "Refresh and rescan",
        "remove_download": "Remove the download",
        "plex_refresh": "Refresh in Plex",
    }
    return words.get(kind, "Do it"), kind == "remove_download"


def perform(settings: ConfigSettings, kind: str, params: dict[str, Any]) -> str:
    """does a confirmed action, returns what happened. Raises on failure"""
    action = ACTIONS.get(kind)
    if action is None:
        raise ActionInvalid(f"unknown action {kind}")
    return action[2](settings, params)
