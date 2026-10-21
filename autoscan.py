"""autoscan.py"""

import logging

import requests

from config.models import AutoscanConfig

logger = logging.getLogger(__name__)


def trigger_autoscan(autoscan_config: AutoscanConfig, scan_dirs: set[str]) -> bool:
    """send folders to autoscan's manual trigger, autoscan handles path rewrites and scan delay.
    returns whether autoscan accepted them"""
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
        return True
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "error"
        hint = " - check autoscan username/password" if status == 401 else ""
        logger.error(
            "AUTOSCAN Failed to trigger scan: HTTP %s (%s)%s", status, url, hint
        )
    except requests.exceptions.Timeout:
        logger.error("AUTOSCAN Failed to trigger scan: timed out (%s)", url)
    except requests.exceptions.ConnectionError:
        logger.error("AUTOSCAN Failed to trigger scan: could not connect (%s)", url)
    except requests.exceptions.RequestException as e:
        logger.error("AUTOSCAN Failed to trigger scan: %s (%s)", e, url)
    return False
