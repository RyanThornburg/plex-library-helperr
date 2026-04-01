"""plex.py"""

import logging

import requests
from plexapi import exceptions as plexapi_exceptions
from plexapi.server import PlexServer

from config.models import PlexConfig

logger = logging.getLogger(__name__)


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
        logger.info("PLEX Updating library: [blue]%s[/blue]", library)
        try:
            plex.library.section(library).update()  # type: ignore[reportUnknownMemberType]
        except plexapi_exceptions.NotFound:
            logger.error("PLEX Library not found: %s", library)


def setup_plex_server(plex_config: PlexConfig) -> PlexServer | None:
    """attempt to connect to plex server"""
    try:
        logger.info("Connecting to Plex Server")
        return PlexServer(plex_config.baseurl, plex_config.token)

    except plexapi_exceptions.PlexApiException, requests.exceptions.RequestException:
        logger.error("Invalid Plex configuration. Please check and try again")
        return None
