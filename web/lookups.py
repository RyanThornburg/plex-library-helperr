"""lookups.py - radarr/sonarr/plex data for the settings pages: choices for rule fields,
connection tests and previews of what a rule matches. Libraries are cached for a few
minutes, they don't change much while a rule is being edited"""

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import requests
from arrapi import Movie, QualityProfile, RadarrAPI, RootFolder, Series, SonarrAPI
from plexapi.server import PlexServer

from config.models import ArrConfig, MoverRule, PlexConfig, ProfileRule
from web.jobs import utc_now

logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(minutes=10)
TIMEOUT = 15
# a preview lists this many titles, and says how many more there are
PREVIEW_LIMIT = 200
ARR_APIS: dict[str, type[RadarrAPI | SonarrAPI]] = {"radarr": RadarrAPI, "sonarr": SonarrAPI}
ARR_MEDIA: dict[str, str] = {"radarr": "all_movies", "sonarr": "all_series"}


class LookupFailed(Exception):
    """a service couldn't be reached or refused, the message has no secrets in it"""


def redact(text: str, *secrets: str | None) -> str:
    """error text without secrets: requests puts urls in it, and tautulli's key is a
    query parameter"""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


@contextmanager
def quiet(logger_name: str) -> Iterator[None]:
    """drop this thread's records of a logger, a preview checks the whole library and
    would log every match"""
    thread = threading.get_ident()

    def other_threads(record: logging.LogRecord) -> bool:
        return record.thread != thread

    target = logging.getLogger(logger_name)
    target.addFilter(other_threads)
    try:
        yield
    finally:
        target.removeFilter(other_threads)


def _sorted(values: Any) -> list[str]:
    return sorted({v for v in values if v}, key=str.casefold)


# what matching rules read from a movie/series, tags and quality profile come from ids
MEDIA_FIELDS = ("id", "title", "path", "added", "genres", "certification", "studio", "network")


def _loaded(media: Movie | Series, name: str) -> Any:
    try:
        return object.__getattribute__(media, name)
    except AttributeError:  # movies have no network, series no studio
        return None


def snapshot(
    media: Movie | Series, tags: dict[int, str], profiles: dict[int, str]
) -> SimpleNamespace:
    """the fields rules match on, read as loaded. arrapi reloads a movie/series from the
    api when a field it read is empty (e.g. no certification), and its tags/profile only
    hold an id until read, one request each. Those come from the arr's lists instead"""
    return SimpleNamespace(
        **{name: _loaded(media, name) for name in MEDIA_FIELDS},
        tags=[
            SimpleNamespace(label=tags.get(i, str(i))) for i in _loaded(media, "tagsIds") or []
        ],
        qualityProfile=SimpleNamespace(
            name=profiles.get(_loaded(media, "qualityProfileId"), "")
        ),
    )


@dataclass
class ArrLibrary:
    """one radarr/sonarr's media and settings, as matching rules see them"""

    # snapshot()s, matching reads them like Movie/Series
    media: list[Any]
    root_folders: list[RootFolder]
    profiles: list[QualityProfile]
    tags: list[str]
    fetched_at: datetime

    @property
    def paths(self) -> list[str]:
        return [r.path for r in self.root_folders]

    @property
    def profile_names(self) -> list[str]:
        return [p.name for p in self.profiles]

    def values(self, name: str) -> list[str]:
        """every value of a media field in the library: genres, certifications, studios,
        networks or titles"""
        if name == "genres":
            return _sorted(g for m in self.media for g in m.genres)
        attribute = {
            "certifications": "certification",
            "studios": "studio",
            "networks": "network",
            "titles": "title",
        }[name]
        return _sorted(getattr(m, attribute, None) for m in self.media)


@dataclass
class Match:
    title: str
    path: str
    added: datetime | None
    # added within filter_time, so a run would change it now
    recent: bool


@dataclass
class Preview:
    matches: list[Match] = field(default_factory=list[Match])
    total: int = 0
    # matches added within filter_time, which the next run would change
    recent: int = 0
    # media an earlier mover matches first, which this one never sees
    claimed: int = 0
    problem: str | None = None


def _matches(media: list[Movie | Series], filter_time: timedelta | None) -> Preview:
    cutoff = utc_now() - filter_time if filter_time else None
    matches = [
        Match(
            m.title,
            m.path,
            m.added,
            cutoff is None or (m.added is not None and m.added >= cutoff),
        )
        for m in sorted(media, key=lambda m: m.title.casefold())
    ]
    return Preview(
        matches=matches[:PREVIEW_LIMIT],
        total=len(matches),
        recent=sum(m.recent for m in matches),
    )


def preview_mover(
    library: ArrLibrary,
    earlier: list[MoverRule],
    rule: MoverRule,
    filter_time: timedelta | None,
    log_name: str,
) -> Preview:
    """what a mover matches in the whole library, after the movers before it (the first
    match wins, like a run). A run only checks media added within filter_time"""
    from main import get_matches_based_on_mover_rules

    if rule.path not in library.paths:
        return Preview(problem=f"{rule.path} isn't a root folder in {log_name.title()}")
    with quiet("main"):
        claimed: set[int] = set()
        for mover in earlier:
            unclaimed = [m for m in library.media if m.id not in claimed]
            claimed.update(
                m.id
                for m in get_matches_based_on_mover_rules(
                    mover, unclaimed, library.root_folders, log_name
                )
            )
        unclaimed = [m for m in library.media if m.id not in claimed]
        matches = get_matches_based_on_mover_rules(
            rule, unclaimed, library.root_folders, log_name
        )
        everything = get_matches_based_on_mover_rules(
            rule, library.media, library.root_folders, log_name
        )
    preview = _matches(matches, filter_time)
    preview.claimed = len(everything) - len(matches)
    return preview


def preview_profiler(
    library: ArrLibrary, rule: ProfileRule, filter_time: timedelta | None, log_name: str
) -> Preview:
    """the media a profiler would change, titles already on the profile aren't counted"""
    from main import get_media_matching_profile_rules

    if rule.profile not in library.profile_names:
        return Preview(
            problem=f"{rule.profile} isn't a quality profile in {log_name.title()}"
        )
    with quiet("main"):
        matches = get_media_matching_profile_rules(
            rule, library.media, library.profiles, log_name
        )
    return _matches(matches, filter_time)


class Lookups:
    def __init__(self) -> None:
        self._cache: dict[tuple[str, ...], tuple[datetime, Any]] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        """forget everything, after a connection changes"""
        with self._lock:
            self._cache.clear()

    def _cached(self, key: tuple[str, ...], fetch: Any, refresh: bool = False) -> Any:
        # one fetch at a time: a second page waits for the first's result
        with self._lock:
            hit = self._cache.get(key)
            if hit and not refresh and utc_now() - hit[0] < CACHE_TTL:
                return hit[1]
            value = fetch()
            self._cache[key] = (utc_now(), value)
            return value

    def arr_library(
        self, arr_name: str, arr: ArrConfig | None, refresh: bool = False
    ) -> ArrLibrary:
        if arr is None or not arr.baseurl or not arr.apikey:
            raise LookupFailed(f"{arr_name.title()} isn't connected (Settings → Connections)")
        baseurl, apikey = arr.baseurl, arr.apikey

        def fetch() -> ArrLibrary:
            try:
                api = ARR_APIS[arr_name](baseurl, apikey)
                tags = {t.id: t.label for t in api.all_tags()}
                profiles = api.quality_profile()
                names = {p.id: p.name for p in profiles}
                return ArrLibrary(
                    media=[
                        snapshot(m, tags, names)
                        for m in getattr(api, ARR_MEDIA[arr_name])()
                    ],
                    root_folders=api.root_folder(),
                    profiles=profiles,
                    tags=_sorted(tags.values()),
                    fetched_at=utc_now(),
                )
            except Exception as e:
                logger.warning("WEB\t Loading %s failed: %s", arr_name, redact(str(e), apikey))
                raise LookupFailed(
                    f"Couldn't load {arr_name.title()}: {redact(str(e), apikey)}"
                ) from None

        return self._cached((arr_name, baseurl), fetch, refresh)

    def plex_libraries(self, plex: PlexConfig | None) -> list[tuple[str, str]]:
        """(title, type) of every plex library"""
        if plex is None or not plex.baseurl or not plex.token:
            raise LookupFailed("Plex isn't connected (Settings → Connections)")
        baseurl, token = plex.baseurl, plex.token

        def fetch() -> list[tuple[str, str]]:
            try:
                server = PlexServer(baseurl, token, timeout=TIMEOUT)
                return [(s.title, s.type) for s in server.library.sections()]
            except Exception as e:
                raise LookupFailed(f"Couldn't load Plex: {redact(str(e), token)}") from None

        return self._cached(("plex", baseurl), fetch)


def check_connection(service: str, values: dict[str, str]) -> str:
    """check a service's url and credentials, returns what answered. Raises LookupFailed"""
    url = values.get("url", "").rstrip("/")
    secret = values.get("secret", "")
    try:
        if service in ARR_APIS:
            response = requests.get(
                f"{url}/api/v3/system/status", headers={"X-Api-Key": secret}, timeout=TIMEOUT
            )
            response.raise_for_status()
            status = response.json()
            return f"{status.get('appName', service.title())} {status.get('version', '')}"
        if service == "plex":
            server = PlexServer(url, secret, timeout=TIMEOUT)
            return f"Plex {server.friendlyName} {server.version}"
        if service == "tautulli":
            response = requests.get(
                f"{url}/api/v2",
                params={"apikey": secret, "cmd": "get_tautulli_info"},
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            body = response.json().get("response", {})
            if body.get("result") != "success":
                raise LookupFailed(f"Tautulli refused: {body.get('message') or 'unknown error'}")
            return f"Tautulli {body.get('data', {}).get('tautulli_version', '')}"
        if service == "seerr":
            # status answers without a key, requests need it
            headers = {"X-Api-Key": secret}
            response = requests.get(f"{url}/api/v1/request", params={"take": 1}, headers=headers, timeout=TIMEOUT)
            if response.status_code in (401, 403):
                raise LookupFailed(f"Seerr refused the API key (HTTP {response.status_code})")
            response.raise_for_status()
            status = requests.get(f"{url}/api/v1/status", headers=headers, timeout=TIMEOUT)
            status.raise_for_status()
            return f"Seerr {status.json().get('version', '')}"
        if service == "sabnzbd":
            # version answers without a key, the queue needs it. The key is a query param
            response = requests.get(
                f"{url}/api",
                params={"mode": "queue", "limit": 1, "output": "json", "apikey": secret},
                timeout=TIMEOUT,
            )
            if response.status_code in (401, 403):
                raise LookupFailed(f"SABnzbd refused the API key (HTTP {response.status_code})")
            response.raise_for_status()
            return f"SABnzbd {response.json().get('queue', {}).get('version', '')}"
        if service == "autoscan":
            username = values.get("username", "")
            response = requests.get(
                f"{url}/triggers/manual",
                auth=(username, secret) if username else None,
                timeout=TIMEOUT,
            )
            if response.status_code == 401:
                raise LookupFailed("Autoscan refused the username/password (HTTP 401)")
            response.raise_for_status()
            return "Autoscan's manual trigger"
    except LookupFailed as e:
        raise LookupFailed(redact(str(e), secret)) from None
    except Exception as e:
        raise LookupFailed(redact(f"{type(e).__name__}: {e}", secret)) from None
    raise LookupFailed(f"unknown service {service}")
