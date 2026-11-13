"""duplicates.py - plan removing the Plex versions of a movie or episode that aren't the
file Radarr/Sonarr track, left behind when an arr re-downloads something it lost (a disk
dropping out) and the old copy comes back. Plans are reviewed and approved like
removers', and approved versions are deleted through Plex (its "Allow media deletion"
setting must be on).

What the October 2026 cleanup taught, as skip rules: keep the file the arr tracks (for
Sonarr only the file an episode links to counts, it keeps stale episodefile records),
never guess when that file isn't exactly one of Plex's versions, skip anything the arr
added lately (an upgrade or a converter at work) or is downloading, and compare lengths
instead of names, since a version a lot longer or shorter may be another episode Plex
matched wrongly or another cut"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from xml.etree.ElementTree import Element

import requests
from plexapi import exceptions as plexapi_exceptions
from plexapi.server import PlexServer

from arr_client import ArrV3
from config import format_duration
from config.models import ArrConfig, ConfigSettings, DuplicatesSettings
from logging_config import failure_text
from plex import setup_plex_server
from removals import Approved, act
from results import Change, JobResult, PlannedFile, PlannedRemoval, Status, utc_now

logger = logging.getLogger(__name__)

LOG_NAME = "PLEX"
# plex's search types, and the arr whose file each kind of item is
KINDS = {"movie": ("movie", 1, "RADARR"), "show": ("episode", 4, "SONARR")}
ARR_LABELS = {"RADARR": "Radarr", "SONARR": "Sonarr"}
# a version this much longer or shorter than the arr's file may be something else
LENGTH_TOLERANCE = 0.10
LENGTH_TOLERANCE_MIN_MS = 2 * 60 * 1000
# S01E01E02, S01E01-E02, S01E01-02: deleting that file in plex takes every episode in it
MULTI_EPISODE = re.compile(r"[Ss]\d+[Ee]\d+(?:-?[Ee]\d+|-\d+)")
# how plans and approvals name a version's item
SERVICE = "PLEX"


class DuplicateRule:
    """removals.act() reads a rule's key (for auto-approve, which duplicates don't have)"""

    key = "plex:duplicates"
    plex_library = None


RULE = DuplicateRule()


@dataclass(frozen=True)
class Version:
    """one of an item's versions in plex (a Media), its files are its parts"""

    media_id: int
    files: tuple[str, ...]
    size: int  # bytes
    duration: int | None  # ms
    resolution: str
    container: str

    @property
    def names(self) -> set[str]:
        return {PurePosixPath(f).name for f in self.files}

    @property
    def label(self) -> str:
        what = " ".join(v for v in (self.resolution, self.container) if v) or "version"
        return f"{what}, {_size(self.size)}"


@dataclass
class PlexItem:
    rating_key: int
    kind: str  # movie or episode
    title: str
    library: str
    versions: list[Version]
    tmdb_id: int | None = None
    imdb_id: str | None = None
    show_key: int | None = None
    season: int | None = None
    episode: int | None = None

    @property
    def arr(self) -> str:
        return "RADARR" if self.kind == "movie" else "SONARR"


@dataclass(frozen=True)
class Tracked:
    """the file the arr tracks for an item"""

    name: str  # the file's name: the arr and plex see different paths
    added: datetime | None
    queued: bool


@dataclass
class Duplicate:
    item: PlexItem
    keep: Version
    remove: list[Version]
    rule: DuplicateRule = RULE

    @property
    def files(self) -> list[PlannedFile]:
        return [PlannedFile(v.media_id, f) for v in self.remove for f in v.files]

    @property
    def labels(self) -> list[str]:
        return [v.label for v in self.remove]

    def planned(self, status: Status, detail: str) -> PlannedRemoval:
        return PlannedRemoval(
            media_id=self.item.rating_key,
            title=self.item.title,
            rule=f"{self.item.library}: keeps {ARR_LABELS[self.item.arr]}'s file ({self.keep.label})",
            labels=self.labels,
            files=self.files,
            status=status,
            detail=detail,
            service=SERVICE,
        )


def _size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _int(value: object) -> int | None:
    try:
        return int(str(value))
    except TypeError, ValueError:
        return None


# ---------------------------------------------------------------------------
# reading plex
# ---------------------------------------------------------------------------


def parse_versions(element: Element) -> list[Version]:
    versions = []
    for media in element.findall("Media"):
        media_id = _int(media.get("id"))
        parts = media.findall("Part")
        files = tuple(str(p.get("file")) for p in parts if p.get("file"))
        if media_id is None or not files:
            continue
        versions.append(
            Version(
                media_id=media_id,
                files=files,
                size=sum(_int(p.get("size")) or 0 for p in parts),
                duration=_int(media.get("duration")),
                resolution=_resolution(media.get("videoResolution")),
                container=str(media.get("container") or ""),
            )
        )
    return versions


def _resolution(value: str | None) -> str:
    if not value:
        return ""
    # plex says 1080, 720, sd, 4k
    return f"{value}p" if value.isdigit() else value.upper()


def guid_ids(element: Element) -> dict[str, str]:
    """an item's agent ids, {"tmdb": "603", "imdb": "tt0133093", "tvdb": "121361"}"""
    found = {}
    for guid in element.findall("Guid"):
        agent, _, value = str(guid.get("id") or "").partition("://")
        if agent and value:
            found[agent] = value
    return found


def parse_item(element: Element, library: str) -> PlexItem | None:
    """a movie or episode with its versions, None when it isn't one"""
    kind = element.get("type")
    rating_key = _int(element.get("ratingKey"))
    if kind not in ("movie", "episode") or rating_key is None:
        return None
    versions = parse_versions(element)
    if kind == "movie":
        ids = guid_ids(element)
        year = element.get("year")
        return PlexItem(
            rating_key, "movie",
            f"{element.get('title')} ({year})" if year else str(element.get("title")),
            library, versions,
            tmdb_id=_int(ids.get("tmdb")), imdb_id=ids.get("imdb"),
        )
    season, episode = _int(element.get("parentIndex")), _int(element.get("index"))
    return PlexItem(
        rating_key, "episode",
        f"{element.get('grandparentTitle')} S{season or 0:02d}E{episode or 0:02d}",
        library, versions,
        show_key=_int(element.get("grandparentRatingKey")), season=season, episode=episode,
    )


class PlexVersions:
    """the plex queries duplicates make"""

    def __init__(self, plex: PlexServer):
        self.plex = plex
        self._shows: dict[int, int | None] = {}

    def sections(self, libraries: list[str]) -> list[tuple[str, str, str]]:
        """(key, kind, title) of each movie/show library to check"""
        found = [
            (str(d.get("key")), str(d.get("type")), str(d.get("title")))
            for d in self.plex.query("/library/sections")
            if d.get("type") in KINDS
        ]
        if libraries:
            wanted = {name.casefold() for name in libraries}
            found = [s for s in found if s[2].casefold() in wanted]
        return found

    def duplicates(self, key: str, kind: str, title: str) -> list[PlexItem]:
        """the library's items with more than one version"""
        _, search_type, _ = KINDS[kind]
        data = self.plex.query(
            f"/library/sections/{key}/all", params={"type": search_type, "duplicate": 1, "includeGuids": 1}
        )
        items = [parse_item(e, title) for e in data or []]
        return [i for i in items if i is not None and len(i.versions) > 1]

    def item(self, rating_key: int) -> PlexItem | None:
        """one item read fresh, None when plex no longer has it"""
        try:
            data = self.plex.query(f"/library/metadata/{rating_key}", params={"includeGuids": 1})
        except plexapi_exceptions.NotFound:
            return None
        element = next(iter(data or []), None)
        if element is None:
            return None
        return parse_item(element, str(data.get("librarySectionTitle") or element.get("librarySectionTitle") or ""))

    def show_tvdb(self, show_key: int) -> int | None:
        if show_key not in self._shows:
            data = self.plex.query(f"/library/metadata/{show_key}", params={"includeGuids": 1})
            element = next(iter(data or []), None)
            self._shows[show_key] = _int(guid_ids(element).get("tvdb")) if element is not None else None
        return self._shows[show_key]

    def delete(self, rating_key: int, media_id: int) -> None:
        """deletes the version's files from disk"""
        self.plex.query(f"/library/metadata/{rating_key}/media/{media_id}", method=self.plex._session.delete)  # noqa: SLF001


# ---------------------------------------------------------------------------
# reading the arrs
# ---------------------------------------------------------------------------


def _date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed if parsed.year >= 1900 else None


class RadarrFiles(ArrV3):
    def __init__(self, arr: ArrConfig):
        super().__init__(arr.baseurl or "", arr.apikey or "")
        self._by_tmdb: dict[int, dict[str, Any]] = {}
        self._by_imdb: dict[str, dict[str, Any]] = {}
        self._queued: set[int] | None = None

    def load(self) -> None:
        for movie in self._request("GET", "movie"):
            if isinstance(movie.get("tmdbId"), int):
                self._by_tmdb[movie["tmdbId"]] = movie
            if movie.get("imdbId"):
                self._by_imdb[str(movie["imdbId"])] = movie

    def tracked(self, item: PlexItem) -> Tracked | None:
        movie = (self._by_tmdb.get(item.tmdb_id) if item.tmdb_id else None) or (
            self._by_imdb.get(item.imdb_id) if item.imdb_id else None
        )
        if movie is None or not movie.get("hasFile"):
            return None
        file = movie.get("movieFile") or {}
        if not file.get("path"):
            return None
        if self._queued is None:
            self._queued = {q["movieId"] for q in self._request("GET", "queue/details") if q.get("movieId") is not None}
        return Tracked(PurePosixPath(file["path"]).name, _date(file.get("dateAdded")), movie["id"] in self._queued)


class SonarrFiles(ArrV3):
    def __init__(self, arr: ArrConfig):
        super().__init__(arr.baseurl or "", arr.apikey or "")
        self._series: dict[int, int] = {}
        self._episodes: dict[int, dict[tuple[int, int], Tracked]] = {}
        self._queued: set[int] | None = None

    def load(self) -> None:
        self._series = {
            s["tvdbId"]: s["id"] for s in self._request("GET", "series") if isinstance(s.get("tvdbId"), int)
        }

    def tracked(self, item: PlexItem, tvdb_id: int | None) -> Tracked | None:
        series_id = self._series.get(tvdb_id) if tvdb_id else None
        if series_id is None or item.season is None or item.episode is None:
            return None
        if series_id not in self._episodes:
            if self._queued is None:
                self._queued = {q["seriesId"] for q in self._request("GET", "queue/details") if q.get("seriesId") is not None}
            files = {f["id"]: f for f in self._request("GET", "episodefile", params={"seriesId": series_id})}
            queued = series_id in self._queued
            episodes: dict[tuple[int, int], Tracked] = {}
            # only the file an episode links to counts: sonarr keeps stale episodefile records
            for episode in self._request("GET", "episode", params={"seriesId": series_id}):
                file = files.get(episode.get("episodeFileId")) if episode.get("hasFile") else None
                if file and file.get("path"):
                    episodes[(episode.get("seasonNumber"), episode.get("episodeNumber"))] = Tracked(
                        PurePosixPath(file["path"]).name, _date(file.get("dateAdded")), queued
                    )
            self._episodes[series_id] = episodes
        return self._episodes[series_id].get((item.season, item.episode))


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def _minutes(ms: int) -> str:
    return f"{round(ms / 60000)} min"


def plan_item(item: PlexItem, tracked: Tracked | None, settings: DuplicatesSettings, now: datetime) -> Duplicate | str:
    """the versions to remove from an item with more than one, or why none are"""
    arr = ARR_LABELS[item.arr]
    if tracked is None:
        return f"not found in {arr}, or {arr} has no file for it"
    if tracked.queued:
        return f"a download for it is in {arr}'s queue"
    if tracked.added is not None and tracked.added > now - settings.min_age:
        return f"{arr}'s file was added in the last {format_duration(settings.min_age)}"
    keep = [v for v in item.versions if tracked.name in v.names]
    if not keep:
        return f"{arr}'s file isn't one of Plex's versions (Plex may not have scanned it yet)"
    if len(keep) > 1:
        return f"more than one version is {arr}'s file"
    kept = keep[0]
    remove = [v for v in item.versions if v is not kept]
    for version in remove:
        if item.kind == "episode" and any(MULTI_EPISODE.search(n) for n in version.names):
            return "a version's file holds more than one episode"
        if kept.duration and version.duration:
            difference = version.duration - kept.duration
            if abs(difference) > max(kept.duration * LENGTH_TOLERANCE, LENGTH_TOLERANCE_MIN_MS):
                longer = "longer" if difference > 0 else "shorter"
                return f"a version is {_minutes(abs(difference))} {longer} than {arr}'s file: it may be another {'episode' if item.kind == 'episode' else 'cut'}, check it in Plex"
    return Duplicate(item, kept, remove)


# ---------------------------------------------------------------------------


def run_duplicates(settings: ConfigSettings, result: JobResult, approved: Approved | None = None) -> None:
    """plan removing the versions of each movie/episode that aren't the arr's file, for
    review. With `approved` (applying a reviewed plan: plex rating key -> media ids) only
    those items are read again from plex and only approved versions that are still
    duplicates are deleted"""
    if settings.plex is None:
        result.errors.append("Plex isn't connected")
        return
    plex = setup_plex_server(settings.plex)
    if plex is None:
        result.errors.append("Could not connect to Plex")
        return
    reader = PlexVersions(plex)
    options = settings.duplicates
    now = utc_now()

    def not_applied(title: str, reason: str) -> None:
        logger.warning("%s\t %s: approved, but not removing (%s)", LOG_NAME, title, reason)
        result.changes.append(Change(LOG_NAME, "remove_version", title, [], "blocked", reason))

    items: list[PlexItem] = []
    if approved is not None:
        logger.info("%s\t Applying an approved plan for %s items", LOG_NAME, len(approved))
        for rating_key in approved:
            item = reader.item(rating_key)
            if item is None:
                not_applied(f"Plex item {rating_key}", "no longer in Plex")
            elif len(item.versions) < 2:
                not_applied(item.title, "it has one version now")
            else:
                items.append(item)
    else:
        for key, kind, title in reader.sections(options.libraries):
            found = reader.duplicates(key, kind, title)
            logger.info("%s\t %s: %s items with more than one version", LOG_NAME, title, len(found))
            items += found

    clients: dict[str, RadarrFiles | SonarrFiles | None] = {}

    def client(arr: str) -> RadarrFiles | SonarrFiles | None:
        if arr not in clients:
            config = settings.radarr if arr == "RADARR" else settings.sonarr
            if config is None or not config.baseurl or not config.apikey:
                clients[arr] = None
            else:
                clients[arr] = RadarrFiles(config) if arr == "RADARR" else SonarrFiles(config)
                clients[arr].load()  # type: ignore[union-attr]
        return clients[arr]

    plans: list[Duplicate] = []
    unchecked: dict[str, int] = {}
    for item in items:
        arr = client(item.arr)
        if arr is None:
            unchecked[item.arr] = unchecked.get(item.arr, 0) + 1
            if approved is not None:
                not_applied(item.title, f"{ARR_LABELS[item.arr]} isn't connected")
            continue
        if isinstance(arr, SonarrFiles):
            tracked = arr.tracked(item, reader.show_tvdb(item.show_key) if item.show_key else None)
        else:
            tracked = arr.tracked(item)
        plan = plan_item(item, tracked, options, now)
        if isinstance(plan, str):
            if approved is not None:
                not_applied(item.title, plan)
            else:
                logger.info("%s\t %s: %s, skipping", LOG_NAME, item.title, plan)
            continue
        if approved is not None:
            chosen = approved[item.rating_key]
            if not chosen & {v.media_id for v in plan.remove}:
                not_applied(item.title, "the approved versions aren't duplicates any more")
                continue
            plan.remove = [v for v in plan.remove if v.media_id in chosen]
        logger.info("%s\t %s: keeping %s, removing %s", LOG_NAME, item.title, plan.keep.label, "; ".join(plan.labels))
        plans.append(plan)
    for arr, count in unchecked.items():
        result.errors.append(f"{ARR_LABELS[arr]} isn't connected, so {count} items with more than one version weren't checked")

    logger.info("%s\t %s items with versions to remove", LOG_NAME, len(plans))
    if not plans:
        return

    def record(plan: Duplicate, status: Status, detail: str) -> None:
        result.changes.append(Change(LOG_NAME, "remove_version", plan.item.title, plan.labels, status, detail))
        result.removals.append(plan.planned(status, detail))

    def delete(plan: Duplicate) -> str | None:
        for done, version in enumerate(plan.remove):
            # an item's other versions may be gone already: say so
            before = f" ({done} of {len(plan.remove)} versions deleted before it)" if done else ""
            try:
                reader.delete(plan.item.rating_key, version.media_id)
            except plexapi_exceptions.Unauthorized:
                return "Plex refused: turn on Settings → Library → Allow media deletion in Plex" + before
            except (plexapi_exceptions.PlexApiException, requests.exceptions.RequestException) as e:
                logger.error("%s\t %s: failed to delete a version (%s)", LOG_NAME, plan.item.title, e)
                return f"delete failed: {failure_text(e)}{before}"
        return None

    act(
        LOG_NAME, "items", plans,
        applying=approved is not None, auto_approve=frozenset(),
        max_deletes=options.max_deletes,
        files=lambda p: len(p.files), record=record, delete=delete, removed=lambda p: None,
    )
