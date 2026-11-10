"""tautulli.py - Tautulli's api (/api/v2), for watch history (the CLI too), the requests
and playback syncs, the status page and the connection Test. Its key is a query
parameter, so a requests error carries it in its url: callers redact before logging or
storing one. TautulliRefused's text never holds it"""

from datetime import datetime, timedelta, timezone
from typing import Any

import requests

TIMEOUT = 60
# rows per get_history page
HISTORY_PAGE = 1000
# plays in progress have no history id yet, so syncs read the last day again
PLAYS_OVERLAP = timedelta(days=1)


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


def tautulli_after(since: datetime) -> str:
    """get_history's `after` for plays since a naive utc time. Tautulli compares it (on or
    after) with each play's date in its own time zone, up to a day behind utc, so the
    day before is asked for and callers check the exact times"""
    return (since - timedelta(days=1)).date().isoformat()


def history_rows(data: object) -> list[dict[str, Any]]:
    """the rows of a get_history page. Anything else raises: read as no plays, it would
    let removers delete what was just watched"""
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError(f"unexpected get_history answer: {type(data).__name__}")
    return rows


def from_timestamp(value: object) -> datetime | None:
    """a Tautulli unix timestamp as naive utc, None when it isn't one"""
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).replace(tzinfo=None)  # type: ignore[arg-type]
    except TypeError, ValueError, OverflowError:
        return None
