"""remover.py - unmonitor and delete stale seasons/episodes from tagged sonarr series"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import requests
from arrapi import Series, SonarrAPI

from arr_client import arr_api
from config.models import ArrConfig, PlexConfig, RemoverRule, TautulliConfig
from results import Change, JobResult, PlannedFile, PlannedRemoval, Status
from watch_history import WatchEvent, get_watch_history

logger = logging.getLogger(__name__)

LOG_NAME = "SONARR"

# an applied plan: series id -> the episode file ids approved for removal
Approved = dict[int, set[int]]


class SonarrEpisodes:
    """episode endpoints arrapi does not cover"""

    def __init__(self, baseurl: str, apikey: str):
        self.url = f"{baseurl.rstrip('/')}/api/v3"
        self.session = requests.Session()
        self.session.headers["X-Api-Key"] = apikey

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, f"{self.url}/{path}", timeout=60, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def episodes(self, series_id: int) -> list[dict[str, Any]]:
        return self._request("GET", "episode", params={"seriesId": series_id})

    def episode_files(self, series_id: int) -> list[dict[str, Any]]:
        return self._request("GET", "episodefile", params={"seriesId": series_id})

    def history(self, series_id: int) -> list[dict[str, Any]]:
        return self._request("GET", "history/series", params={"seriesId": series_id})

    def queued_series_ids(self) -> set[int]:
        return {
            q["seriesId"]
            for q in self._request("GET", "queue/details")
            if q.get("seriesId") is not None
        }

    def unmonitor_episodes(self, episode_ids: list[int]) -> None:
        self._request(
            "PUT", "episode/monitor", json={"episodeIds": episode_ids, "monitored": False}
        )

    def delete_episode_files(self, file_ids: list[int]) -> None:
        self._request("DELETE", "episodefile/bulk", json={"episodeFileIds": file_ids})


class InvalidSeriesData(Exception):
    """sonarr returned missing/invalid data for a series, it is skipped"""


@dataclass
class EpisodeFile:
    id: int
    season: int
    date_added: datetime
    path: str


@dataclass
class Episode:
    id: int
    season: int
    episode: int
    file_id: int | None


def utc_now() -> datetime:
    """naive utc, matching arrapi's series.added"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_naive(value: object) -> datetime:
    if not isinstance(value, str):
        raise InvalidSeriesData(f"invalid date '{value}'")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as e:
        raise InvalidSeriesData(f"invalid date '{value}'") from e
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidSeriesData(f"missing/invalid '{key}': {value!r}")
    return value


def parse_series_data(
    raw_episodes: list[dict[str, Any]], raw_files: list[dict[str, Any]]
) -> tuple[list[Episode], dict[int, EpisodeFile]]:
    """validate sonarr episode/file data, raises InvalidSeriesData rather than guessing"""
    files: dict[int, EpisodeFile] = {}
    for f in raw_files:
        file = EpisodeFile(
            id=_int(f, "id"),
            season=_int(f, "seasonNumber"),
            date_added=_utc_naive(f.get("dateAdded")),
            path=f.get("path") or "",
        )
        if not file.path:
            raise InvalidSeriesData(f"episode file {file.id} has no path")
        files[file.id] = file

    episodes: list[Episode] = []
    for e in raw_episodes:
        file_id = _int(e, "episodeFileId") if e.get("hasFile") else None
        if file_id is not None and file_id not in files:
            raise InvalidSeriesData(f"episode {e.get('id')} references missing file {file_id}")
        episodes.append(
            Episode(
                id=_int(e, "id"),
                season=_int(e, "seasonNumber"),
                episode=_int(e, "episodeNumber"),
                file_id=file_id,
            )
        )
    return episodes, files


IMPORT_EVENTS = {"downloadFolderImported", "seriesFolderImported"}
# a file sonarr lost and found again (a converter swapping it, a disk dropping out) gets a
# new dateAdded, as does an upgrade, neither makes the episode new
KEEPS_AGE = {"MissingFromDisk", "Upgrade"}


def first_imports(history: list[dict[str, Any]]) -> dict[int, datetime]:
    """episode id -> its first import since its file was last deleted on purpose"""
    events: list[tuple[datetime, str, int, str | None]] = []
    for h in history:
        event = h.get("eventType")
        if event in IMPORT_EVENTS or event == "episodeFileDeleted":
            reason = (h.get("data") or {}).get("reason")
            events.append((_utc_naive(h.get("date")), event, _int(h, "episodeId"), reason))
    first: dict[int, datetime] = {}
    for date, event, episode_id, reason in sorted(events, key=lambda e: e[0]):
        if event in IMPORT_EVENTS:
            first.setdefault(episode_id, date)
        elif reason not in KEEPS_AGE:
            first.pop(episode_id, None)
    return first


def added_at(
    file: EpisodeFile, episodes: list[Episode], imported: dict[int, datetime]
) -> datetime:
    """when the file's episodes were first imported, else the file's own dateAdded"""
    dates = [imported.get(e.id) for e in episodes if e.file_id == file.id]
    if dates and all(dates):
        return min(file.date_added, max(d for d in dates if d))
    return file.date_added


def code(season: int, episode: int | None = None) -> str:
    return f"S{season:02d}" + (f"E{episode:02d}" if episode is not None else "")


@dataclass
class Unit:
    """the smallest thing removed as a whole: a season, or an episode file"""

    label: str
    position: tuple[int, int]
    special: bool
    file_ids: set[int]
    episodes: list[Episode]


def _candidate_units(
    rule: RemoverRule, episodes: list[Episode], files: dict[int, EpisodeFile]
) -> list[Unit]:
    """everything outside the keep window, specials never count toward keep"""
    with_files = sorted(
        (e for e in episodes if e.file_id is not None),
        key=lambda e: (e.season, e.episode),
    )
    numbered = [e for e in with_files if e.season > 0]
    specials = [e for e in with_files if e.season == 0] if rule.include_specials else []
    units: list[Unit] = []

    if rule.type == "seasons":
        seasons = sorted({e.season for e in numbered})
        candidate_seasons = seasons[: -rule.keep] if len(seasons) > rule.keep else []
        if specials:
            candidate_seasons = [0, *candidate_seasons]
        for s in candidate_seasons:
            season_eps = [e for e in with_files if e.season == s]
            units.append(
                Unit(
                    label=code(s),
                    position=(s, 0),
                    special=s == 0,
                    file_ids={e.file_id for e in season_eps if e.file_id is not None},
                    episodes=season_eps,
                )
            )
        return units

    # episodes: a file holding any kept episode (multi-episode files) is kept
    kept_file_ids = {e.file_id for e in numbered[-rule.keep :]}
    by_file: dict[int, list[Episode]] = {}
    for e in specials + numbered:
        if e.file_id is not None and e.file_id not in kept_file_ids:
            by_file.setdefault(e.file_id, []).append(e)
    for file_id, eps in by_file.items():
        first = eps[0]
        units.append(
            Unit(
                label=code(first.season, first.episode),
                position=(first.season, first.episode),
                special=first.season == 0,
                file_ids={file_id},
                episodes=eps,
            )
        )
    return units


@dataclass
class RemovalPlan:
    series: Series
    rule: RemoverRule
    units: list[Unit]
    paths: dict[int, str] = field(default_factory=dict[int, str])  # file id -> path

    @property
    def labels(self) -> list[str]:
        return [u.label for u in self.units]

    @property
    def file_ids(self) -> list[int]:
        return sorted({f for u in self.units for f in u.file_ids})

    @property
    def episode_ids(self) -> list[int]:
        return sorted({e.id for u in self.units for e in u.episodes})

    @property
    def dirs(self) -> set[str]:
        return {str(PurePosixPath(self.paths[f]).parent) for f in self.file_ids}

    def restricted_to(self, file_ids: set[int]) -> "RemovalPlan | None":
        """only the units whose every file is in file_ids, so an approved season whose
        files changed since is kept whole rather than partly removed"""
        units = [u for u in self.units if u.file_ids <= file_ids]
        return RemovalPlan(self.series, self.rule, units, self.paths) if units else None

    def planned(self, status: Status, detail: str) -> PlannedRemoval:
        rule = self.rule
        return PlannedRemoval(
            media_id=self.series.id,
            title=self.series.title,
            rule=f"{rule.type}, keep {rule.keep} ({', '.join(rule.tags)})",
            labels=self.labels,
            files=[PlannedFile(f, self.paths[f]) for f in self.file_ids],
            status=status,
            detail=detail,
        )


def plan_series_removal(
    rule: RemoverRule,
    series: Series,
    episodes: list[Episode],
    files: dict[int, EpisodeFile],
    watches: list[WatchEvent] | None,
    now: datetime,
    imported: dict[int, datetime] | None = None,
) -> RemovalPlan | None:
    """pure: decide which seasons/episode files of one series are stale. `imported` is
    each episode's first import (`first_imports()`), which ages a file Sonarr re-found or
    upgraded from when its episode first arrived"""
    candidates = _candidate_units(rule, episodes, files)

    # watch protection looks at every candidate, a viewer on a new season still protects
    # the older seasons after it
    protected: set[str] = set()
    if rule.skip_if_watched_within and candidates:
        watch_cutoff = now - rule.skip_if_watched_within
        recent = {
            (w.season, w.episode) for w in watches or [] if w.watched_at >= watch_cutoff
        }
        # only watches of removal candidates count, watching the latest episodes never blocks
        watched = [
            u
            for u in candidates
            if any((e.season, e.episode) in recent for e in u.episodes)
        ]
        watched_numbered = [u.position for u in watched if not u.special]
        earliest = min(watched_numbered) if watched_numbered else None
        specials_watched = any(u.special for u in watched)
        protected = {
            u.label
            for u in candidates
            if (u.special and specials_watched)
            or (not u.special and earliest is not None and u.position >= earliest)
        }

    # every file in the unit must be old enough (seasons are all or nothing)
    age_cutoff = now - rule.min_age
    old_enough = [
        u
        for u in candidates
        if all(
            added_at(files[f], u.episodes, imported or {}) <= age_cutoff
            for f in u.file_ids
        )
    ]
    held = [u.label for u in old_enough if u.label in protected]
    if held:
        logger.info(
            "%s\t %s: recently watched, protecting %s",
            LOG_NAME,
            series.title,
            ", ".join(held),
        )
    units = [u for u in old_enough if u.label not in protected]

    if not units:
        return None

    return RemovalPlan(
        series=series,
        rule=rule,
        units=units,
        paths={f: files[f].path for u in units for f in u.file_ids},
    )


def waiting_on_age(
    rule: RemoverRule,
    episodes: list[Episode],
    files: dict[int, EpisodeFile],
    now: datetime,
) -> bool:
    """a candidate file is too new by its own dateAdded, so history could change the plan"""
    cutoff = now - rule.min_age
    return any(
        files[f].date_added > cutoff
        for u in _candidate_units(rule, episodes, files)
        for f in u.file_ids
    )


def match_remover(
    series: Series, removers: list[RemoverRule]
) -> RemoverRule | None:
    """the single remover for a series, more than one is a config error"""
    series_tags = {t.label.casefold() for t in series.tags}
    matches = [
        r
        for r in removers
        if series_tags & {t.casefold() for t in r.tags}
        and not series_tags & {t.casefold() for t in r.exclude_tags}
    ]
    if len(matches) > 1:
        logger.error(
            "%s\t %s: tagged for %s removers, fix the series tags. Skipping",
            LOG_NAME,
            series.title,
            len(matches),
        )
        return None
    return matches[0] if matches else None


def series_watches(series: Series, watches: list[WatchEvent]) -> list[WatchEvent]:
    """match by tvdb id, title only when the history had no tvdb id"""
    title = series.title.casefold()
    return [
        w
        for w in watches
        if (series.tvdbId and w.tvdb_id == series.tvdbId)
        or (w.tvdb_id is None and w.title.casefold() == title)
    ]


def skip_reason(
    series: Series,
    rule: RemoverRule,
    watches: list[WatchEvent] | None,
    queued: set[int],
    now: datetime,
) -> str | None:
    """why a tagged series isn't planned this run, None when it is"""
    if rule.skip_if_watched_within and watches is None:
        return "no watch history"
    if series.added is None:
        return "no added date"
    if series.added > now - rule.min_age:
        return "added recently"
    if series.id in queued:
        return "downloads in queue"
    return None


def run_removers(
    arr_config: ArrConfig,
    tautulli: TautulliConfig | None,
    plex: PlexConfig | None,
    result: JobResult,
    approved: Approved | None = None,
    auto_approve: frozenset[str] = frozenset(),
) -> None:
    """plan every removal first, then unmonitor and delete what's approved. Only rules
    whose key is in `auto_approve` delete, the rest are planned for review.

    With `approved` (applying a reviewed plan) only those series are planned again from
    fresh data, and only units whose files were all approved and are still eligible are
    removed.

    max_deletes caps the files a run deletes, planned-only removals don't count"""
    if not arr_config.apikey or not arr_config.baseurl:
        logger.warning("%s not configured. Skipping...", LOG_NAME)
        result.errors.append(f"{LOG_NAME} not configured")
        return
    if not arr_config.removers:
        logger.info("%s\t No removers configured", LOG_NAME)
        return

    settings = arr_config.remover_settings
    now = utc_now()
    arr = arr_api(SonarrAPI, arr_config.baseurl, arr_config.apikey)
    client = SonarrEpisodes(arr_config.baseurl, arr_config.apikey)

    def not_applied(title: str, reason: str, labels: list[str] | None = None) -> None:
        logger.warning("%s\t %s: approved, but not removing (%s)", LOG_NAME, title, reason)
        result.changes.append(Change(LOG_NAME, "remove", title, labels or [], "blocked", reason))

    logger.info("%s\t Reading all media...", LOG_NAME)
    all_series: list[Series] = arr.all_series()
    tagged = [(s, r) for s in all_series if (r := match_remover(s, arr_config.removers))]
    if approved is not None:
        logger.info("%s\t Applying an approved plan for %s series", LOG_NAME, len(approved))
        tagged = [(s, r) for s, r in tagged if s.id in approved]
        titles = {s.id: s.title for s in all_series}
        for series_id in approved.keys() - {s.id for s, _ in tagged}:
            if series_id in titles:
                not_applied(titles[series_id], "no longer tagged for one remover")
            else:
                not_applied(f"series {series_id}", "no longer in Sonarr")
    logger.info("%s\t %s series tagged for removers", LOG_NAME, len(tagged))
    if not tagged:
        return

    watches: list[WatchEvent] | None = None
    windows = [r.skip_if_watched_within for _, r in tagged if r.skip_if_watched_within]
    if windows:
        watches = get_watch_history(tautulli, plex, now - max(windows))
        if watches is None:
            logger.error(
                "%s\t No watch history, skipping series with skip_if_watched_within",
                LOG_NAME,
            )
            result.errors.append(
                "No watch history, skipped series with skip_if_watched_within"
            )

    queued = client.queued_series_ids()

    plans: list[RemovalPlan] = []
    for series, rule in tagged:
        if reason := skip_reason(series, rule, watches, queued, now):
            if approved is not None:
                not_applied(series.title, reason)
            elif reason != "no watch history":  # already logged once for the run
                logger.info("%s\t %s: %s, skipping", LOG_NAME, series.title, reason)
            continue
        try:
            episodes, files = parse_series_data(
                client.episodes(series.id), client.episode_files(series.id)
            )
            imported = (
                first_imports(client.history(series.id))
                if waiting_on_age(rule, episodes, files, now)
                else {}
            )
        except InvalidSeriesData as e:
            logger.warning("%s\t %s: invalid data, skipping (%s)", LOG_NAME, series.title, e)
            if approved is not None:
                not_applied(series.title, f"invalid Sonarr data: {e}")
            continue

        plan = plan_series_removal(
            rule,
            series,
            episodes,
            files,
            series_watches(series, watches) if watches else [],
            now,
            imported,
        )
        if approved is not None:
            approved_files = approved[series.id]
            plan = plan.restricted_to(approved_files) if plan else None
            left = len(approved_files - set(plan.file_ids if plan else []))
            if left:
                not_applied(
                    series.title,
                    f"{left} of {len(approved_files)} approved files no longer eligible "
                    "(watched, replaced or changed since the plan)",
                )
        if plan:
            logger.info(
                "%s\t %s: removing %s (%s files)",
                LOG_NAME,
                series.title,
                ", ".join(plan.labels),
                len(plan.file_ids),
            )
            plans.append(plan)

    total = sum(len(p.file_ids) for p in plans)
    logger.info("%s\t %s files to remove from %s series", LOG_NAME, total, len(plans))
    if not plans:
        return

    def record(plan: RemovalPlan, status: Status, detail: str = "") -> None:
        files = f"{len(plan.file_ids)} files"
        detail = f"{files}, {detail}" if detail else files
        result.changes.append(
            Change(LOG_NAME, "remove", plan.series.title, plan.labels, status, detail)
        )
        result.removals.append(plan.planned(status, detail))

    # approved and auto-approved removals delete, the rest wait for review
    auto = approved is None
    live = [p for p in plans if not auto or p.rule.key in auto_approve]
    review = [p for p in plans if p not in live]
    if review:
        logger.info(
            "%s\t DRY RUN - planning %s series for review, not removing",
            LOG_NAME,
            len(review),
        )
        for p in review:
            record(p, "dry_run")
    if not live:
        return
    if auto:
        logger.info("%s\t Auto-approved: removing %s series", LOG_NAME, len(live))

    live_total = sum(len(p.file_ids) for p in live)
    if settings.max_deletes is not None and live_total > settings.max_deletes:
        logger.error(
            "%s\t %s files exceeds max_deletes (%s), not removing anything",
            LOG_NAME,
            live_total,
            settings.max_deletes,
        )
        detail = (
            f"run total of {live_total} files exceeds max_deletes ({settings.max_deletes})"
        )
        for p in live:
            record(p, "blocked", detail)
        return

    for plan in live:
        try:
            # unmonitor first so sonarr does not grab the deleted files again
            client.unmonitor_episodes(plan.episode_ids)
        except requests.exceptions.RequestException as e:
            logger.error("%s\t %s: failed to unmonitor (%s)", LOG_NAME, plan.series.title, e)
            record(plan, "failed", f"unmonitor failed: {e}")
            continue
        try:
            client.delete_episode_files(plan.file_ids)
        except requests.exceptions.RequestException as e:
            logger.error("%s\t %s: failed to delete (%s)", LOG_NAME, plan.series.title, e)
            record(plan, "failed", f"unmonitored, but delete failed: {e}")
            continue
        record(plan, "done", "auto-approved" if auto else "")
        result.scan_dirs.update(plan.dirs)
        if plan.rule.plex_library:
            result.plex_libraries.add(plan.rule.plex_library)
