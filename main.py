"""
Helper script for organizing sonarr/radarr libraries with plex
"""

import logging.config
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Callable, Iterable

from arrapi import (
    Movie,
    QualityProfile,
    RadarrAPI,
    RootFolder,
    Series,
    SonarrAPI,
)

import config
from config.models import ArrConfig, MoverRule, PlexConfig, ProfileRule
from logging_config import LOGGING_CONFIG
from plex import refresh_plex_libraries

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


def get_media_id(media: Movie | Series, requester: str) -> int | str:
    """return the id of the media"""
    if requester.lower() == "sonarr":
        return media.tvdbId if media.tvdbId else media.id

    if media.tmdbId:
        return media.tmdbId

    if media.imdbId:
        return media.imdbId

    return media.id


def get_matches_based_on_mover_rules(
    mover: MoverRule,
    media: list[Movie | Series],
    root_folders: list[RootFolder],
    log_name: str,
) -> list[int | str]:
    """Check genres with conditional inputs to move to different library"""
    media_ids: list[int | str] = []

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
        fm_id = get_media_id(fm, log_name)
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
        media_ids.append(fm_id)

    return media_ids


def get_media_matching_profile_rules(
    profile: ProfileRule,
    media: list[Movie | Series],
    quality_profiles: list[QualityProfile],
    log_name: str,
) -> list[int | str]:
    """Check media titles/studios/network and update profile"""
    media_ids: list[int | str] = []

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
        "%s\t Checking %s files for profiler: [blue]%s[/blue]",
        log_name,
        len(filtered_media),
        profile.profile,
    )
    for fm in filtered_media:
        fm_id = get_media_id(fm, log_name)

        studio_match = bool(
            studios_set and fm.studio and fm.studio.casefold() in studios_set
        )
        network_match = bool(
            networks_set and fm.network and fm.network.casefold() in networks_set
        )
        title_match = bool(titles_set and fm.title.casefold() in titles_set)

        if title_match or studio_match or network_match:
            logger.info("%s\t Adding %s to profiler list", log_name, fm.title)
            media_ids.append(fm_id)

    return media_ids


def main() -> None:
    """Update radarr/sonarr library"""

    logging.config.dictConfig(LOGGING_CONFIG)
    logger.info("Loading config settings")
    settings = config.load_config()
    logger.info(settings)

    plex_libraries: set[str] = set()

    arr_dispatch: list[
        tuple[ArrConfig | None, type[RadarrAPI | SonarrAPI], str, str, str]
    ] = [
        (settings.radarr, RadarrAPI, "all_movies", "edit_multiple_movies", "RADARR"),
        (settings.sonarr, SonarrAPI, "all_series", "edit_multiple_series", "SONARR"),
    ]

    for arr_config, api_cls, all_fn, edit_fn, log_name in arr_dispatch:
        logger.info("-" * 90)
        logger.info("[yellow bold]STARTING %s[/yellow bold]", log_name)
        if arr_config is None or not arr_config.apikey or not arr_config.baseurl:
            logger.warning("%s not configured. Skipping...", log_name)
            continue
        arr = api_cls(arr_config.baseurl, arr_config.apikey)
        logger.info("%s\t Reading all media...", log_name)
        media: list[Movie | Series] = getattr(arr, all_fn)()
        logger.info("%s\t Total Files: %s", log_name, len(media))

        if settings.filter_time:
            logger.info("%s\t Filtering media...", log_name)
            filter_time = (
                datetime.now(timezone.utc).replace(tzinfo=None) - settings.filter_time
            )
            media = [m for m in media if m.added >= filter_time]
            logger.info("%s\t Total Files: %s", log_name, len(media))

        root_folders: list[RootFolder] = arr.root_folder()
        quality_profiles: list[QualityProfile] = arr.quality_profile()

        if arr_config.movers:
            logger.info("%s\t Mover checks starting", log_name)
            for mover in arr_config.movers:
                media_ids = get_matches_based_on_mover_rules(
                    mover, media, root_folders, log_name
                )
                if media_ids:
                    logger.info(
                        "%s\t Moving %s files to root: %s",
                        log_name,
                        len(media_ids),
                        mover.path,
                    )
                    if not arr_config.dry_run:
                        if mover.plex_library:
                            plex_libraries.add(mover.plex_library)
                        getattr(arr, edit_fn)(
                            ids=media_ids, root_folder=mover.path, move_files=True
                        )
                    else:
                        logger.info(
                            "%s\t [red]DRY RUN[/red] - not actually moving", log_name
                        )
                else:
                    logger.info(
                        "%s\t [sandy_brown]No media found matching rule[/sandy_brown]",
                        log_name,
                    )

        if arr_config.profilers:
            logger.info("%s\t Profilers starting", log_name)
            for p in arr_config.profilers:
                media_ids = get_media_matching_profile_rules(
                    p, media, quality_profiles, log_name
                )
                if media_ids:
                    logger.info(
                        "%s\t Updating %s media profiles to %s",
                        log_name,
                        len(media_ids),
                        p.profile,
                    )
                    if not arr_config.dry_run:
                        getattr(arr, edit_fn)(
                            ids=media_ids,
                            quality_profile=p.profile,
                            monitored=p.monitored,
                        )
                    else:
                        logger.info(
                            "%s\t [red]DRY RUN[/red] - not actually moving", log_name
                        )
                else:
                    logger.info(
                        "%s\t [sandy_brown]No matches found for profiler[/sandy_brown]",
                        log_name,
                    )

    logger.info("-" * 90)
    # CALL PLEX
    if plex_libraries:
        plex_config: PlexConfig | None = settings.plex
        refresh_plex_libraries(
            plex_config,
            plex_libraries,
        )
        logger.info("-" * 90)

    logger.info("FINISHED")
    logger.info("=" * 90)


if __name__ == "__main__":
    main()
