"""autoscan.py"""

import logging

import requests

from config.models import AutoscanConfig

logger = logging.getLogger(__name__)


def trigger_autoscan(autoscan_config: AutoscanConfig, scan_dirs: set[str]) -> None:
    """send folders to autoscan's manual trigger, autoscan handles path rewrites and scan delay"""
    url = f"{autoscan_config.url.rstrip('/')}/triggers/manual"
    auth = (
        (autoscan_config.username, autoscan_config.password or "")
        if autoscan_config.username
        else None
    )

    for d in sorted(scan_dirs):
        logger.info("AUTOSCAN Queuing scan: %s", d)

    try:
        response = requests.post(
            url,
            params=[("dir", d) for d in sorted(scan_dirs)],
            auth=auth,
            timeout=30,
        )
        response.raise_for_status()
    except requests.exceptions.RequestException:
        logger.exception("AUTOSCAN Failed to trigger scan")
