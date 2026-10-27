"""movie_remover.py - delete old movies (and their files) from radarr root folders"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

import requests

from config import format_duration
from config.models import ArrConfig, MovieRemoverRule
from results import Change, JobResult, PlannedFile, PlannedRemoval, Status

logger = logging.getLogger(__name__)

LOG_NAME = "RADARR"

# an applied plan: movie id -> the movie file ids approved for removal
Approved = dict[int, set[int]]


class RadarrMovies:
    """the radarr endpoints removers use, arrapi's Movie has no releaseDate or file"""

    def __init__(self, baseurl: str, apikey: str):
        self.url = f"{baseurl.rstrip('/')}/api/v3"
        self.session = requests.Session()
        self.session.headers["X-Api-Key"] = apikey

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.session.request(method, f"{self.url}/{path}", timeout=60, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    def movies(self) -> list[dict[str, Any]]:
        return self._request("GET", "movie")

    def tags(self) -> dict[int, str]:
        return {t["id"]: t["label"] for t in self._request("GET", "tag")}

    def queued_movie_ids(self) -> set[int]:
        return {
            q["movieId"]
            for q in self._request("GET", "queue/details")
            if q.get("movieId") is not None
        }

    def delete_movie(self, movie_id: int) -> None:
        """the movie and its files, without an import exclusion"""
        self._request(
            "DELETE",
            f"movie/{movie_id}",
            params={"deleteFiles": "true", "addImportExclusion": "false"},
        )


class InvalidMovieData(Exception):
    """radarr returned missing/invalid data for a movie, it is skipped"""


@dataclass(frozen=True)
class MovieFile:
    id: int
    path: str


@dataclass
class RadarrMovie:
    id: int
    title: str
    year: int | None
    path: str  # the movie's folder
    root: str  # its root folder, without a trailing /
    added: datetime
    released: datetime | None
    tags: set[str]  # labels, casefolded
    tmdb_id: int | None
    imdb_id: str | None
    file: MovieFile | None

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


# when each movie (by radarr id) was played, by anyone, however long ago. Movies whose
# plays can't be known (e.g. not found in plex) are left out, and it raises when there's
# no play history at all
PlayLookup = Callable[[list[RadarrMovie]], dict[int, list[datetime]]]


def utc_now() -> datetime:
    """naive utc, like the parsed radarr dates"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _date(value: object) -> datetime | None:
    """a radarr date as naive utc. Radarr writes 0001-01-01 for a date it doesn't
    have, which must never read as very old"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed if parsed.year >= 1900 else None


def _int(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidMovieData(f"missing/invalid '{key}': {value!r}")
    return value


def parse_movie(raw: dict[str, Any], tag_labels: dict[int, str]) -> RadarrMovie:
    """validate radarr's movie data, raises InvalidMovieData rather than guessing"""
    movie_id = _int(raw, "id")
    title = raw.get("title")
    path = raw.get("path")
    if not isinstance(title, str) or not title:
        raise InvalidMovieData("no title")
    if not isinstance(path, str) or not path:
        raise InvalidMovieData("no path")
    root = raw.get("rootFolderPath") or str(PurePosixPath(path).parent)
    added = _date(raw.get("added"))
    if added is None:
        raise InvalidMovieData(f"invalid added date {raw.get('added')!r}")

    file = None
    if raw.get("hasFile"):
        movie_file = raw.get("movieFile")
        if not isinstance(movie_file, dict):
            raise InvalidMovieData("has a file, but no movieFile")
        file_path = movie_file.get("path")
        if not isinstance(file_path, str) or not file_path:
            raise InvalidMovieData("its file has no path")
        file = MovieFile(_int(movie_file, "id"), file_path)

    year = raw.get("year")
    tmdb_id = raw.get("tmdbId")
    return RadarrMovie(
        id=movie_id,
        title=title,
        year=year if isinstance(year, int) and year > 0 else None,
        path=path,
        root=root.rstrip("/"),
        added=added,
        released=_date(raw.get("releaseDate")),
        tags={
            tag_labels[t].casefold() for t in raw.get("tags") or [] if t in tag_labels
        },
        tmdb_id=tmdb_id if isinstance(tmdb_id, int) and tmdb_id > 0 else None,
        imdb_id=raw.get("imdbId") or None,
        file=file,
    )


def match_remover(
    movie: RadarrMovie, removers: list[MovieRemoverRule]
) -> MovieRemoverRule | None:
    """the remover of the movie's root folder, the config allows one per folder"""
    return next((r for r in removers if r.root == movie.root), None)


def age_skip_reason(
    movie: RadarrMovie, rule: MovieRemoverRule, queued: set[int], now: datetime
) -> str | None:
    """why a movie in a remover's folder isn't a candidate, before its plays are
    looked up. None when it is"""
    if movie.file is None:
        return "no file"
    if {t.casefold() for t in rule.exclude_tags} & movie.tags:
        return "excluded tag"
    if movie.added > now - rule.min_age:
        return "added recently"
    if movie.released is None:
        return "no release date"
    if movie.released > now - rule.min_age:
        return "released recently"
    if movie.id in queued:
        return "download in queue"
    return None


def watch_skip_reason(
    rule: MovieRemoverRule, plays: list[datetime] | None, now: datetime
) -> str | None:
    """why a candidate is kept because of its plays, None when it can go. Unknown
    plays keep it when the rule checks them"""
    if not rule.watch_check:
        return None
    if plays is None:
        return "plays unknown"
    if rule.skip_if_ever_watched and plays:
        return "watched"
    if rule.skip_if_watched_within and any(
        p > now - rule.skip_if_watched_within for p in plays
    ):
        return "watched recently"
    return None


def labels(movie: RadarrMovie, plays: list[datetime] | None) -> list[str]:
    """what a reviewer needs to see about the movie"""
    assert movie.released is not None
    result = [f"released {movie.released:%Y-%m-%d}", f"added {movie.added:%Y-%m-%d}"]
    if plays is not None:
        result.append(f"last played {max(plays):%Y-%m-%d}" if plays else "never played")
    return result


@dataclass
class MovieRemoval:
    movie: RadarrMovie
    rule: MovieRemoverRule
    labels: list[str]

    @property
    def file(self) -> MovieFile:
        assert self.movie.file is not None
        return self.movie.file

    def planned(self, status: Status, detail: str) -> PlannedRemoval:
        return PlannedRemoval(
            media_id=self.movie.id,
            title=self.movie.label,
            rule=f"older than {format_duration(self.rule.min_age)} in {self.rule.root}",
            labels=self.labels,
            files=[PlannedFile(self.file.id, self.file.path)],
            status=status,
            detail=detail,
            service=LOG_NAME,
        )


def run_movie_removers(
    arr_config: ArrConfig,
    result: JobResult,
    approved: Approved | None = None,
    auto_approve: frozenset[str] = frozenset(),
    plays: PlayLookup | None = None,
) -> None:
    """plan every removal first, then delete what's approved, like remover.run_removers.
    Only rules whose key is in `auto_approve` delete, the rest are planned for review.

    With `approved` (applying a reviewed plan) only those movies are planned again from
    fresh data, and only removed when their file is the one approved.

    `plays` looks up when movies were played. Without it (only the web app has play
    history) rules that check plays skip every movie"""
    if not arr_config.apikey or not arr_config.baseurl:
        logger.warning("%s not configured. Skipping...", LOG_NAME)
        result.errors.append(f"{LOG_NAME} not configured")
        return
    removers = [r for r in arr_config.removers if isinstance(r, MovieRemoverRule)]
    if not removers:
        logger.info("%s\t No removers configured", LOG_NAME)
        return

    now = utc_now()
    client = RadarrMovies(arr_config.baseurl, arr_config.apikey)

    def not_applied(title: str, reason: str) -> None:
        logger.warning("%s\t %s: approved, but not removing (%s)", LOG_NAME, title, reason)
        result.changes.append(Change(LOG_NAME, "remove", title, [], "blocked", reason))

    logger.info("%s\t Reading all media...", LOG_NAME)
    tag_labels = client.tags()
    movies: list[RadarrMovie] = []
    invalid: set[object] = set()
    for raw in client.movies():
        try:
            movies.append(parse_movie(raw, tag_labels))
        except InvalidMovieData as e:
            title = raw.get("title") or f"movie {raw.get('id')}"
            logger.warning("%s\t %s: invalid data, skipping (%s)", LOG_NAME, title, e)
            invalid.add(raw.get("id"))
            if approved is not None and raw.get("id") in approved:
                not_applied(title, f"invalid Radarr data: {e}")

    matched = [(m, r) for m in movies if (r := match_remover(m, removers))]
    if approved is not None:
        logger.info("%s\t Applying an approved plan for %s movies", LOG_NAME, len(approved))
        matched = [(m, r) for m, r in matched if m.id in approved]
        found = {m.id for m, _ in matched}
        titles = {m.id: m.label for m in movies}
        for movie_id in approved.keys() - found - invalid:
            if movie_id in titles:
                not_applied(titles[movie_id], "no longer in a remover's root folder")
            else:
                not_applied(f"movie {movie_id}", "no longer in Radarr")
    logger.info("%s\t %s movies in removers' root folders", LOG_NAME, len(matched))
    if not matched:
        return

    queued = client.queued_movie_ids()
    candidates: list[tuple[RadarrMovie, MovieRemoverRule]] = []
    too_new = 0
    for movie, rule in matched:
        reason = age_skip_reason(movie, rule, queued, now)
        if reason is None:
            candidates.append((movie, rule))
        elif approved is not None:
            not_applied(movie.label, reason)
        elif reason in ("no file", "added recently", "released recently"):
            # most of a folder, every run: counted rather than logged one by one
            if reason != "no file":
                too_new += 1
            logger.debug("%s\t %s: %s, skipping", LOG_NAME, movie.label, reason)
        else:
            logger.info("%s\t %s: %s, skipping", LOG_NAME, movie.label, reason)
    if too_new:
        logger.info("%s\t %s movies not old enough yet", LOG_NAME, too_new)

    watched: dict[int, list[datetime]] = {}
    checked = [m for m, r in candidates if r.watch_check]
    if checked:
        try:
            if plays is None:
                raise ValueError("play history is only available in the web app")
            watched = plays(checked)
        except Exception as e:  # any failure keeps every movie whose plays count
            logger.error(
                "%s\t No play history, skipping movies of removers that check plays (%s)",
                LOG_NAME,
                e,
            )
            result.errors.append(
                f"No play history, skipped movies of removers that check plays: {e}"
            )

    removals: list[MovieRemoval] = []
    for movie, rule in candidates:
        movie_plays = watched.get(movie.id)
        if reason := watch_skip_reason(rule, movie_plays, now):
            if approved is not None:
                not_applied(movie.label, reason)
            else:
                logger.info("%s\t %s: %s, skipping", LOG_NAME, movie.label, reason)
            continue
        removal = MovieRemoval(movie, rule, labels(movie, movie_plays))
        if approved is not None and removal.file.id not in approved[movie.id]:
            not_applied(movie.label, "its file was replaced since the plan")
            continue
        logger.info("%s\t %s: removing (%s)", LOG_NAME, movie.label, ", ".join(removal.labels))
        removals.append(removal)

    logger.info("%s\t %s movies to remove", LOG_NAME, len(removals))
    if not removals:
        return

    def record(removal: MovieRemoval, status: Status, detail: str = "") -> None:
        result.changes.append(
            Change(LOG_NAME, "remove", removal.movie.label, removal.labels, status, detail)
        )
        result.removals.append(removal.planned(status, detail))

    # approved and auto-approved removals delete, the rest wait for review
    auto = approved is None
    live = [r for r in removals if not auto or r.rule.key in auto_approve]
    review = [r for r in removals if r not in live]
    if review:
        logger.info(
            "%s\t DRY RUN - planning %s movies for review, not removing",
            LOG_NAME,
            len(review),
        )
        for r in review:
            record(r, "dry_run")
    if not live:
        return
    if auto:
        logger.info("%s\t Auto-approved: removing %s movies", LOG_NAME, len(live))

    max_deletes = arr_config.remover_settings.max_deletes
    if max_deletes is not None and len(live) > max_deletes:
        logger.error(
            "%s\t %s files exceeds max_deletes (%s), not removing anything",
            LOG_NAME,
            len(live),
            max_deletes,
        )
        detail = f"run total of {len(live)} files exceeds max_deletes ({max_deletes})"
        for r in live:
            record(r, "blocked", detail)
        return

    for removal in live:
        try:
            client.delete_movie(removal.movie.id)
        except requests.exceptions.RequestException as e:
            logger.error("%s\t %s: failed to delete (%s)", LOG_NAME, removal.movie.label, e)
            record(removal, "failed", f"delete failed: {e}")
            continue
        record(removal, "done", "auto-approved" if auto else "")
        result.scan_dirs.add(removal.movie.path)
        if removal.rule.plex_library:
            result.plex_libraries.add(removal.rule.plex_library)
