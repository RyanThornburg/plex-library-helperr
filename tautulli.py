"""tautulli.py - Tautulli's api (/api/v2), for watch history (the CLI too), the requests
and playback syncs, the status page and the connection Test. Its key is a query
parameter, so a requests error carries it in its url: callers redact before logging or
storing one. TautulliRefused's text never holds it"""

from typing import Any

import requests

TIMEOUT = 60


class TautulliRefused(Exception):
    """Tautulli answered, but refused: the key, or a command that failed. Safe to show"""


def tautulli_get(url: str, apikey: str, cmd: str, timeout: float = TIMEOUT, **params: object) -> Any:
    """the data of one command. Raises TautulliRefused, or requests' errors (e.g. 400 for
    a rating key plex no longer has)"""
    response = requests.get(
        f"{url.rstrip('/')}/api/v2", params={"apikey": apikey, "cmd": cmd, **params}, timeout=timeout
    )
    if response.status_code in (401, 403):
        raise TautulliRefused(f"Tautulli refused the API key (HTTP {response.status_code})")
    response.raise_for_status()
    body = response.json().get("response") or {}
    if body.get("result") != "success":
        raise TautulliRefused(f"Tautulli {cmd} failed: {body.get('message') or 'unknown error'}")
    return body.get("data")
