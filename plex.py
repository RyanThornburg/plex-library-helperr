"""plex.py"""

import logging

import requests
from plexapi import exceptions as plexapi_exceptions
from plexapi import utils as plexapi_utils
from plexapi.library import LibrarySection
from plexapi.server import PlexServer

from config.models import EmptyTrashSettings, PlexConfig

logger = logging.getLogger(__name__)

# trash is counted by the items that hold files, a show is never trashed on its own
TRASH_LIBTYPES: dict[str, str] = {"movie": "movie", "show": "episode"}


def refresh_plex_libraries(plex_config: PlexConfig | None, plex_libraries: set[str]):
    """refresh plex libraries"""

    if not plex_config:
        logger.warning("Plex server not configured")
        return

    # setup server
    plex = setup_plex_server(plex_config)
    if not plex:
        return

    # update libraries
    for library in plex_libraries:
        logger.info("PLEX Updating library: %s", library)
        try:
            plex.library.section(library).update()  # type: ignore[reportUnknownMemberType]
        except plexapi_exceptions.NotFound:
            logger.error("PLEX Library not found: %s", library)


def empty_plex_trash(
    plex_config: PlexConfig | None, settings: EmptyTrashSettings
) -> None:
    """empty plex library trash, skipping libraries that look like a missing mount"""

    if not plex_config:
        logger.warning("Plex server not configured")
        return

    plex = setup_plex_server(plex_config)
    if not plex:
        return

    try:
        sections = get_trash_sections(plex, settings.libraries)
    except (plexapi_exceptions.PlexApiException, requests.exceptions.RequestException) as e:
        logger.error("PLEX Failed to load libraries (%s)", e)
        return

    for section in sections:
        try:
            empty_section_trash(plex, section, settings)
        except (
            plexapi_exceptions.PlexApiException,
            requests.exceptions.RequestException,
        ) as e:
            logger.error("PLEX %s: failed to empty trash (%s)", section.title, e)


def get_trash_sections(plex: PlexServer, libraries: list[str]) -> list[LibrarySection]:
    """configured libraries, or every movie/show library when none are configured"""
    if not libraries:
        return [s for s in plex.library.sections() if s.type in TRASH_LIBTYPES]

    sections: list[LibrarySection] = []
    for library in libraries:
        try:
            sections.append(plex.library.section(library))
        except plexapi_exceptions.NotFound:
            logger.error("PLEX Library not found: %s", library)
    return sections


def count_trash(plex: PlexServer, section: LibrarySection) -> int | None:
    """number of items with deleted media, including old versions left by upgrades"""
    args = {
        "type": plexapi_utils.searchType(TRASH_LIBTYPES[section.type]),
        "trash": 1,
        "X-Plex-Container-Start": 0,
        "X-Plex-Container-Size": 0,
    }
    data = plex.query(f"/library/sections/{section.key}/all{plexapi_utils.joinArgs(args)}")
    return plexapi_utils.cast(int, data.attrib.get("totalSize"))


def empty_section_trash(
    plex: PlexServer, section: LibrarySection, settings: EmptyTrashSettings
) -> None:
    """empty one library's trash if it is not scanning and is under max_items"""
    if section.type not in TRASH_LIBTYPES:
        logger.warning(
            "PLEX %s: %s libraries are not supported, skipping",
            section.title,
            section.type,
        )
        return
    # a scan in progress may not have trashed everything yet, the next run gets it
    if section.refreshing:
        logger.warning("PLEX %s: scan in progress, skipping", section.title)
        return

    count = count_trash(plex, section)
    if count is None:
        logger.error("PLEX %s: could not count trash, skipping", section.title)
        return
    if count == 0:
        logger.info("PLEX %s: trash is empty", section.title)
        return
    if count > settings.max_items:
        logger.warning(
            "PLEX %s: %s items in trash is over max_items (%s), skipping. "
            "Check the library's files are reachable before raising max_items",
            section.title,
            count,
            settings.max_items,
        )
        return

    logger.info("PLEX %s: emptying trash (%s items)", section.title, count)
    if settings.dry_run:
        logger.info("PLEX DRY RUN - not actually emptying trash")
        return
    section.emptyTrash()


def setup_plex_server(plex_config: PlexConfig) -> PlexServer | None:
    """attempt to connect to plex server"""
    try:
        logger.info("Connecting to Plex Server")
        return PlexServer(plex_config.baseurl, plex_config.token)

    except plexapi_exceptions.PlexApiException, requests.exceptions.RequestException:
        logger.error("Invalid Plex configuration. Please check and try again")
        return None
