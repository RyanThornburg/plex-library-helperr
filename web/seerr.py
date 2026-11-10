"""seerr.py - Seerr's (Overseerr's) api (/api/v1), for the requests sync, the status page
and the connection Test. SeerrRefused's text is safe to show"""

from typing import Any

import requests

TIMEOUT = 60


class SeerrRefused(Exception):
    """Seerr refused the API key"""


def seerr_get(url: str, apikey: str, path: str, timeout: float = TIMEOUT, **params: object) -> Any:
    response = requests.get(
        f"{url.rstrip('/')}/api/v1/{path}", params=params or None, headers={"X-Api-Key": apikey}, timeout=timeout
    )
    if response.status_code in (401, 403):
        raise SeerrRefused(f"Seerr refused the API key (HTTP {response.status_code})")
    response.raise_for_status()
    return response.json()
