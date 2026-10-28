"""
Helper script for organizing sonarr/radarr libraries with plex
"""

import argparse
import logging.config
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Callable, Iterable

import requests
from arrapi import (
    ArrException,
    Movie,
    QualityProfile,
    RadarrAPI,
    RootFolder,
    Series,
    SonarrAPI,
)

from arr_client import arr_api
from autoscan import trigger_autoscan
from config.models import ArrConfig, ConfigSettings, MoverRule, ProfileRule
from logging_config import logging_config, redacted
from plex import empty_plex_trash, refresh_plex_libraries
from movie_remover import PlayLookup, run_movie_removers
from remover import Approved, run_removers
from results import JOBS, Change, JobResult

MATCH_FN: dict[str, Callable[[Iterable[object]], bool]] = {"any": any, "all": all}

logger = logging.getLogger(__name__)


def is_profile_valid(profile_name: str, quality_profiles: list[QualityProfile]) -> bool:
    """check provided profile is valid vs existing"""
    return any(q.name == profile_name for q in quality_profiles)


def is_path_valid(current_path: str, root_folders: list[RootFolder]) -> bool:
    """check provided path is valid vs existing"""
    return any(r.path == current_path for r in root_folders)


def filter_media(media: list[Movie | Series], mover: MoverRule) -> list[Movie | Series]:
    """trying to be os safe on paths, untested on windows"""
    target = PurePosixPath(mover.path)
    exclude_paths = [PurePosixPath(ep) for ep in mover.exclude_paths]

    filtered_media: list[Movie | Series] = []

    for m in media:
        media_path = PurePosixPath(m.path)

        in_target = media_path == target or target in media_path.parents
        in_excluded = any(
            media_path == ep or ep in media_path.parents for ep in exclude_paths
        )

        if not in_target and not in_excluded:
            filtered_media.append(m)

    return filtered_media


def get_moved_dirs(
    matches: list[Movie | Series],
    updated: list[Movie | Series],
    target: str,
) -> set[str]:
    """old and new folders for moved media, to scan both source and target libraries"""
    new_paths = {u.id: u.path for u in updated}
    dirs: set[str] = set()
    for m in matches:
        dirs.add(m.path)
        # arr returns the updated path, fall back to target root + current folder name
        dirs.add(
            new_paths.get(m.id) or str(PurePosixPath(target) / PurePosixPath(m.path).name)
        )
    return dirs


def get_matches_based_on_mover_rules(
    mover: MoverRule,
    media: list[Movie | Series],
    root_folders: list[RootFolder],
    log_name: str,
) -> list[Movie | Series]:
    """Check genres with conditional inputs to move to different library"""
    matches: list[Movie | Series] = []

    # validation
    if not is_path_valid(mover.path, root_folders):
        logger.warning("%s\t Skipping, path does not exist: %s", log_name, mover.path)
        return []

    # filter files not currently in the desired path
    filtered_media: list[Movie | Series] = filter_media(media, mover)

    if not filtered_media:
        logger.info(
            "%s\t No media to check for mover: %s",
            log_name,
            mover,
        )
        return []

    logger.info(
        "%s\t Checking %s files for mover: %s", log_name, len(filtered_media), mover
    )

    genres_set = {g.casefold() for g in mover.genres}
    exclude_genres_set = {g.casefold() for g in mover.exclude_genres}
    studios_set = {s.casefold() for s in mover.studios}
    networks_set = {s.casefold() for s in mover.networks}
    certifications_set = {c.casefold() for c in mover.certifications}
    exclude_certifications_set = {e.casefold() for e in mover.exclude_certifications}
    tags_set = {t.casefold() for t in mover.tags}
    exclude_tags_set = {e.casefold() for e in mover.exclude_tags}

    for fm in filtered_media:
        media_genres = {g.casefold() for g in fm.genres}
        media_tags = {t.label.casefold() for t in fm.tags}

        # excludes
        if exclude_tags_set and exclude_tags_set & media_tags:
            continue

        if exclude_genres_set and exclude_genres_set & media_genres:
            continue

        if (
            exclude_certifications_set
            and fm.certification
            and fm.certification.casefold() in exclude_certifications_set
        ):
            continue

        #####################
        # checks
        # studio check (radarr)
        if studios_set and (not fm.studio or fm.studio.casefold() not in studios_set):
            continue

        # network checks (sonarr)
        if networks_set and (
            not fm.network or fm.network.casefold() not in networks_set
        ):
            continue

        # cert check
        if certifications_set and (
            not fm.certification
            or fm.certification.casefold() not in certifications_set
        ):
            continue

        # genre check
        if genres_set and not MATCH_FN[mover.genre_match](
            g in media_genres for g in genres_set
        ):
            continue

        # tag check
        if tags_set and not MATCH_FN[mover.tag_match](
            t in media_tags for t in tags_set
        ):
            continue

        logger.info("%s\t Adding %s to mover list", log_name, fm.title)
        matches.append(fm)

    return matches


def get_media_matching_profile_rules(
    profile: ProfileRule,
    media: list[Movie | Series],
    quality_profiles: list[QualityProfile],
    log_name: str,
) -> list[Movie | Series]:
    """Check media titles/studios/network and update profile"""
    matches: list[Movie | Series] = []

    # validation
    if not is_profile_valid(profile.profile, quality_profiles):
        logger.warning("%s\t Profile does not exist: %s", log_name, profile.profile)
        return []

    filtered_media: list[Movie | Series] = [
        m for m in media if m.qualityProfile.name != profile.profile
    ]
    if not filtered_media:
        logger.info(
            "%s\t No media to check not using profile %s", log_name, profile.profile
        )
        return []

    titles_set = {t.casefold() for t in profile.titles}
    studios_set = {s.casefold() for s in profile.studios}
    networks_set = {s.casefold() for s in profile.networks}

    logger.info(
        "%s\t Checking %s files for profiler: %s",
        log_name,
        len(filtered_media),
        profile.profile,
    )
    for fm in filtered_media:

        studio_match = bool(
            studios_set and fm.studio and fm.studio.casefold() in studios_set
        )
        network_match = bool(
            networks_set and fm.network and fm.network.casefold() in networks_set
        )
        title_match = bool(titles_set and fm.title.casefold() in titles_set)

        if title_match or studio_match or network_match:
            logger.info("%s\t Adding %s to profiler list", log_name, fm.title)
            matches.append(fm)

    return matches


def run_arr(
    arr_config: ArrConfig,
    api_cls: type[RadarrAPI | SonarrAPI],
    all_fn: str,
    edit_fn: str,
    log_name: str,
    filter_time: timedelta | None,
    result: JobResult,
) -> None:
    """Run movers and profilers against a single radarr/sonarr instance"""
    if not arr_config.apikey or not arr_config.baseurl:
        logger.warning("%s not configured. Skipping...", log_name)
        return

    arr = arr_api(api_cls, arr_config.baseurl, arr_config.apikey)
    logger.info("%s\t Reading all media...", log_name)
    media: list[Movie | Series] = getattr(arr, all_fn)()
    logger.info("%s\t Total Files: %s", log_name, len(media))

    if filter_time:
        logger.info("%s\t Filtering media...", log_name)
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - filter_time
        media = [m for m in media if m.added >= cutoff]
        logger.info("%s\t Total Files: %s", log_name, len(media))

    root_folders: list[RootFolder] = arr.root_folder()
    quality_profiles: list[QualityProfile] = arr.quality_profile()

    if arr_config.movers:
        logger.info("%s\t Mover checks starting", log_name)
        # first matching mover wins, later movers skip already matched media
        claimed_ids: set[int] = set()
        for mover in arr_config.movers:
            unclaimed = [m for m in media if m.id not in claimed_ids]
            matches = get_matches_based_on_mover_rules(
                mover, unclaimed, root_folders, log_name
            )
            if matches:
                claimed_ids.update(m.id for m in matches)
                logger.info(
                    "%s\t Moving %s files to root: %s",
                    log_name,
                    len(matches),
                    mover.path,
                )
                titles = [m.title for m in matches]
                if not arr_config.dry_run:
                    updated, _ = getattr(arr, edit_fn)(
                        ids=matches,
                        root_folder=mover.path,
                        move_files=True,
                        **(
                            {"tags": arr_config.tag, "apply_tags": "add"}
                            if arr_config.tag
                            else {}
                        ),
                    )
                    result.changes.append(
                        Change(log_name, "move", mover.path, titles, "done")
                    )
                    if mover.plex_library:
                        result.plex_libraries.add(mover.plex_library)
                    result.scan_dirs.update(get_moved_dirs(matches, updated, mover.path))
                else:
                    logger.info("%s\t DRY RUN - not actually moving", log_name)
                    result.changes.append(
                        Change(log_name, "move", mover.path, titles, "dry_run")
                    )
            else:
                logger.info(
                    "%s\t No media found matching rule",
                    log_name,
                )

    if arr_config.profilers:
        logger.info("%s\t Profilers starting", log_name)
        for p in arr_config.profilers:
            matches = get_media_matching_profile_rules(
                p, media, quality_profiles, log_name
            )
            if matches:
                logger.info(
                    "%s\t Updating %s media profiles to %s",
                    log_name,
                    len(matches),
                    p.profile,
                )
                titles = [m.title for m in matches]
                if not arr_config.dry_run:
                    getattr(arr, edit_fn)(
                        ids=matches,
                        quality_profile=p.profile,
                        monitored=p.monitored,
                    )
                    result.changes.append(
                        Change(log_name, "profile", p.profile, titles, "done")
                    )
                else:
                    logger.info("%s\t DRY RUN - not actually updating", log_name)
                    result.changes.append(
                        Change(log_name, "profile", p.profile, titles, "dry_run")
                    )
            else:
                logger.info(
                    "%s\t No matches found for profiler",
                    log_name,
                )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    jobs = parser.add_mutually_exclusive_group()
    jobs.add_argument(
        "--remove",
        action="store_true",
        help="run removers (plan removing stale sonarr seasons/episodes and old radarr movies) instead of movers/profilers",
    )
    jobs.add_argument(
        "--empty-trash",
        action="store_true",
        help="empty plex library trash instead of running movers/profilers",
    )
    jobs.add_argument(
        "--serve",
        action="store_true",
        help="run the web app instead of a job (port from PORT, default 8080)",
    )
    return parser.parse_args(argv)


def serve() -> None:
    """one worker on purpose: the job scheduler lives in this process"""
    import uvicorn

    from web.settings import load_web_settings

    try:
        load_web_settings()
    except ValueError as e:
        logger.error("%s", e)
        sys.exit(1)

    uvicorn.run(
        "web.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        workers=1,
        log_config=None,
        server_header=False,
    )


def load_settings() -> ConfigSettings:
    """the web app's stored config, the same one its jobs run with"""
    from web.config_store import open_store

    return open_store().load_settings()


def failed(e: Exception, message: str) -> None:
    """logs a failure: an arr's error says what went wrong (its traceback would only add
    the url, with the api key in it), anything else gets its traceback"""
    if isinstance(e, (ArrException, requests.exceptions.RequestException)):
        logger.error("%s: %s", message, e)
    else:
        logger.exception(message)


def run_all_removers(
    settings: ConfigSettings,
    result: JobResult,
    approved: Mapping[str, Approved] | None,
    auto_approve: frozenset[str],
    movie_plays: PlayLookup | None,
) -> None:
    """sonarr's then radarr's removers. Applying a plan only touches the arrs it approved
    media of. Radarr removers are optional: radarr is skipped quietly without them"""
    any_removers = any(a and a.removers for a in (settings.sonarr, settings.radarr))
    for arr_config, log_name in ((settings.sonarr, "SONARR"), (settings.radarr, "RADARR")):
        arr_approved = None if approved is None else approved.get(log_name)
        if approved is not None and not arr_approved:
            continue
        if approved is None and log_name == "RADARR" and not (arr_config and arr_config.removers):
            continue
        if arr_config is None:
            # an error when it was needed: applying its media, or no removers anywhere
            if approved is not None or not any_removers:
                logger.warning("%s not configured. Skipping...", log_name)
                result.errors.append(f"{log_name} not configured")
            continue
        logger.info("-" * 90)
        logger.info("STARTING %s REMOVERS", log_name)
        try:
            if log_name == "SONARR":
                run_removers(
                    arr_config,
                    settings.tautulli,
                    settings.plex,
                    result,
                    arr_approved,
                    auto_approve,
                )
            else:
                run_movie_removers(arr_config, result, arr_approved, auto_approve, movie_plays)
        except Exception as e:
            # one arr failing doesn't stop the other, and what was removed stays recorded
            failed(e, f"{log_name}\t Removers failed")
            result.errors.append(f"{log_name} removers failed: {redacted(str(e))}")


def run_job(
    job: str,
    settings: ConfigSettings,
    approved: Mapping[str, Approved] | None = None,
    auto_approve: frozenset[str] = frozenset(),
    removed_files: Mapping[str, int] | None = None,
    movie_plays: PlayLookup | None = None,
) -> JobResult:
    """run one job (movers, removers or empty_trash) and return what it changed.
    `approved` applies a reviewed removers plan ({"SONARR"/"RADARR": approved}) and
    `auto_approve` holds the keys of removers that delete without review, see
    remover.run_removers and movie_remover.run_movie_removers, which `movie_plays` tells
    when movies were played. `removed_files` maps file names the removers deleted to
    their run, see plex.empty_plex_trash"""
    if job not in JOBS:
        raise ValueError(f"unknown job '{job}', expected one of {', '.join(JOBS)}")
    if approved is not None and job != "removers":
        raise ValueError("only a removers plan can be approved")
    result = JobResult(job)

    if job == "empty_trash":
        logger.info("-" * 90)
        logger.info("STARTING PLEX EMPTY TRASH")
        try:
            empty_plex_trash(settings.plex, settings.empty_trash, result, removed_files)
        except Exception as e:
            failed(e, "PLEX\t Emptying the trash failed")
            result.errors.append(f"Emptying the trash failed: {redacted(str(e))}")
        return result

    if job == "removers":
        run_all_removers(settings, result, approved, auto_approve, movie_plays)
        scan_changed(settings, result)
        return result

    arr_dispatch: list[
        tuple[ArrConfig | None, type[RadarrAPI | SonarrAPI], str, str, str]
    ] = [
        (settings.radarr, RadarrAPI, "all_movies", "edit_multiple_movies", "RADARR"),
        (settings.sonarr, SonarrAPI, "all_series", "edit_multiple_series", "SONARR"),
    ]

    for arr_config, api_cls, all_fn, edit_fn, log_name in arr_dispatch:
        logger.info("-" * 90)
        logger.info("STARTING %s", log_name)
        if arr_config is None:
            logger.warning("%s not configured. Skipping...", log_name)
            continue
        try:
            run_arr(
                arr_config,
                api_cls,
                all_fn,
                edit_fn,
                log_name,
                settings.filter_time,
                result,
            )
        except Exception as e:
            failed(e, f"{log_name}\t Failed, skipping remaining rules")
            result.errors.append(f"{log_name} failed, skipped remaining rules: {redacted(str(e))}")

    scan_changed(settings, result)
    return result


def main(argv: list[str] | None = None) -> JobResult | None:
    """Run one job and return its result, or serve the web app"""

    args = parse_args(argv)
    logging.config.dictConfig(logging_config("web.log" if args.serve else "log.log"))
    if args.serve:
        serve()
        return None
    logger.info("Loading config settings")
    settings = load_settings()
    logger.info(settings)

    job = "removers" if args.remove else "empty_trash" if args.empty_trash else "movers"
    result = run_job(job, settings)

    logger.info("-" * 90)
    result.log_summary()
    logger.info("-" * 90)
    logger.info("FINISHED")
    logger.info("=" * 90)
    return result


def scan_changed(settings: ConfigSettings, result: JobResult) -> None:
    """scan changed folders with autoscan, or refresh plex libraries without it"""
    try:
        _scan_changed(settings, result)
    except Exception as e:
        # the changes already made stay in the result
        failed(e, "SCAN\t Scanning the changed folders failed")
        result.errors.append(f"Scanning the changed folders failed: {redacted(str(e))}")


def _scan_changed(settings: ConfigSettings, result: JobResult) -> None:
    if settings.autoscan:
        if result.scan_dirs:
            logger.info("-" * 90)
            ok = trigger_autoscan(settings.autoscan, result.scan_dirs)
            result.changes.append(
                Change(
                    "AUTOSCAN",
                    "scan",
                    "autoscan",
                    sorted(result.scan_dirs),
                    "done" if ok else "failed",
                )
            )
    elif result.plex_libraries:
        logger.info("-" * 90)
        refreshed = refresh_plex_libraries(settings.plex, result.plex_libraries)
        for library in sorted(result.plex_libraries):
            result.changes.append(
                Change(
                    "PLEX",
                    "refresh",
                    library,
                    [],
                    "done" if library in refreshed else "failed",
                )
            )


if __name__ == "__main__":
    job_result = main()
    sys.exit(1 if job_result is not None and job_result.failed else 0)
