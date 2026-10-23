"""plex.py"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from xml.etree.ElementTree import Element

import requests
from plexapi import exceptions as plexapi_exceptions
from plexapi import utils as plexapi_utils
from plexapi.library import LibrarySection
from plexapi.server import PlexServer

from config.models import EmptyTrashSettings, PlexConfig
from results import Change, JobResult, Status, TrashedItem

logger = logging.getLogger(__name__)

# trash is counted by the items that hold files, a show is never trashed on its own
TRASH_LIBTYPES: dict[str, str] = {"movie": "movie", "show": "episode"}


def refresh_plex_libraries(
    plex_config: PlexConfig | None, plex_libraries: set[str]
) -> set[str]:
    """refresh plex libraries, returns the ones refreshed"""

    if not plex_config:
        logger.warning("Plex server not configured")
        return set()

    # setup server
    plex = setup_plex_server(plex_config)
    if not plex:
        return set()

    # update libraries
    refreshed: set[str] = set()
    for library in plex_libraries:
        logger.info("PLEX Updating library: %s", library)
        try:
            plex.library.section(library).update()  # type: ignore[reportUnknownMemberType]
            refreshed.add(library)
        except plexapi_exceptions.NotFound:
            logger.error("PLEX Library not found: %s", library)
    return refreshed


def empty_plex_trash(
    plex_config: PlexConfig | None,
    settings: EmptyTrashSettings,
    result: JobResult,
    removed_files: Mapping[str, int] | None = None,
) -> None:
    """empty plex library trash, skipping libraries that look like a missing mount.
    removed_files maps the names of files the web app's removers deleted to their run,
    those don't count toward max_deleted_items"""

    if not plex_config:
        logger.warning("Plex server not configured")
        result.errors.append("Plex server not configured")
        return

    plex = setup_plex_server(plex_config)
    if not plex:
        result.errors.append("Could not connect to Plex")
        return

    try:
        sections = get_trash_sections(plex, settings.libraries, result)
    except (plexapi_exceptions.PlexApiException, requests.exceptions.RequestException) as e:
        logger.error("PLEX Failed to load libraries (%s)", e)
        result.errors.append(f"Failed to load Plex libraries: {e}")
        return

    for section in sections:
        try:
            change = empty_section_trash(plex, section, settings, removed_files or {}, result)
        except (
            plexapi_exceptions.PlexApiException,
            requests.exceptions.RequestException,
        ) as e:
            logger.error("PLEX %s: failed to empty trash (%s)", section.title, e)
            change = Change("PLEX", "empty_trash", section.title, [], "failed", str(e))
        if change:
            result.changes.append(change)


def get_trash_sections(
    plex: PlexServer, libraries: list[str], result: JobResult
) -> list[LibrarySection]:
    """configured libraries, or every movie/show library when none are configured"""
    if not libraries:
        return [s for s in plex.library.sections() if s.type in TRASH_LIBTYPES]

    sections: list[LibrarySection] = []
    for library in libraries:
        try:
            sections.append(plex.library.section(library))
        except plexapi_exceptions.NotFound:
            logger.error("PLEX Library not found: %s", library)
            result.errors.append(f"Plex library not found: {library}")
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


@dataclass
class SectionTrash:
    # items with a version still there, left by upgrades (and moves Plex matched up)
    upgraded: int = 0
    deleted: list[TrashedItem] = field(default_factory=list[TrashedItem])

    def unexpected(self) -> int:
        """fully deleted items no removers run explains, what a missing mount looks like"""
        return sum(1 for t in self.deleted if t.removed_by_run is None)

    def summary(self) -> str:
        by_removers = len(self.deleted) - self.unexpected()
        deleted = f"{len(self.deleted)} fully deleted"
        if by_removers:
            deleted += f" ({by_removers} by removers)"
        return f"{self.upgraded} old versions after upgrades, {deleted}"


def item_title(item: Element) -> str:
    if item.get("type") == "episode":
        return (
            f"{item.get('grandparentTitle')} - "
            f"S{int(item.get('parentIndex') or 0):02d}E{int(item.get('index') or 0):02d}"
            f" - {item.get('title')}"
        )
    year = item.get("year")
    return f"{item.get('title')} ({year})" if year else str(item.get("title"))


def list_trash(
    plex: PlexServer, section: LibrarySection, removed_files: Mapping[str, int]
) -> SectionTrash:
    """sort a library's trash by whether each item keeps a version. Plex marks each
    trashed version (Media) with deletedAt"""
    args = {"type": plexapi_utils.searchType(TRASH_LIBTYPES[section.type]), "trash": 1}
    data = plex.query(f"/library/sections/{section.key}/all{plexapi_utils.joinArgs(args)}")
    trash = SectionTrash()
    for item in data:
        media = item.findall("Media")
        if any(not m.get("deletedAt") for m in media):
            trash.upgraded += 1
            continue
        files = [str(p.get("file")) for m in media for p in m.findall("Part") if p.get("file")]
        # sonarr and plex see different paths for the same file, its name is the same
        runs = [removed_files[n] for f in files if (n := PurePosixPath(f).name) in removed_files]
        trash.deleted.append(
            TrashedItem(section.title, item_title(item), files, max(runs) if runs else None)
        )
    return trash


def empty_section_trash(
    plex: PlexServer,
    section: LibrarySection,
    settings: EmptyTrashSettings,
    removed_files: Mapping[str, int],
    result: JobResult,
) -> Change | None:
    """empty one library's trash if it is not scanning, is under max_items and its fully
    deleted items are explained or under max_deleted_items. None when there's nothing to do.
    Fully deleted items are added to result.trash"""

    def change(status: Status, detail: str, items: list[str] | None = None) -> Change:
        return Change("PLEX", "empty_trash", section.title, items or [], status, detail)

    if section.type not in TRASH_LIBTYPES:
        logger.warning(
            "PLEX %s: %s libraries are not supported, skipping",
            section.title,
            section.type,
        )
        return None
    # a scan in progress may not have trashed everything yet, the next run gets it
    if section.refreshing:
        logger.warning("PLEX %s: scan in progress, skipping", section.title)
        return change("blocked", "scan in progress")

    count = count_trash(plex, section)
    if count is None:
        logger.error("PLEX %s: could not count trash, skipping", section.title)
        return change("failed", "could not count trash")
    if count == 0:
        logger.info("PLEX %s: trash is empty", section.title)
        return None
    if count > settings.max_items:
        logger.warning(
            "PLEX %s: %s items in trash is over max_items (%s), skipping. "
            "Check the library's files are reachable before raising max_items",
            section.title,
            count,
            settings.max_items,
        )
        return change(
            "blocked", f"{count} items in trash is over max_items ({settings.max_items})"
        )

    trash = list_trash(plex, section, removed_files)
    result.trash.extend(trash.deleted)
    titles = [t.title for t in trash.deleted]
    for t in trash.deleted:
        logger.info(
            "PLEX %s: fully deleted: %s%s",
            section.title,
            t.title,
            f" (removers run {t.removed_by_run})" if t.removed_by_run else "",
        )
    if trash.unexpected() > settings.max_deleted_items:
        logger.warning(
            "PLEX %s: %s fully deleted items not removed by removers is over "
            "max_deleted_items (%s), skipping. Check the library's files are reachable",
            section.title,
            trash.unexpected(),
            settings.max_deleted_items,
        )
        return change(
            "blocked",
            f"{trash.summary()}: {trash.unexpected()} not by removers is over "
            f"max_deleted_items ({settings.max_deleted_items})",
            titles,
        )

    logger.info("PLEX %s: emptying trash, %s", section.title, trash.summary())
    if settings.dry_run:
        logger.info("PLEX DRY RUN - not actually emptying trash")
        return change("dry_run", trash.summary(), titles)
    section.emptyTrash()
    return change("done", trash.summary(), titles)


def setup_plex_server(plex_config: PlexConfig) -> PlexServer | None:
    """attempt to connect to plex server"""
    try:
        logger.info("Connecting to Plex Server")
        return PlexServer(plex_config.baseurl, plex_config.token)

    except plexapi_exceptions.PlexApiException, requests.exceptions.RequestException:
        logger.error("Invalid Plex configuration. Please check and try again")
        return None
