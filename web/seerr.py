"""seerr.py - Seerr's (Overseerr's) api (/api/v1), for the requests sync, the status page,
adding requests and the connection Test. SeerrRefused's and SeerrDeclined's text is safe
to show"""

from typing import Any

import requests

TIMEOUT = 60


class SeerrRefused(Exception):
    """Seerr refused the API key"""


class SeerrDeclined(Exception):
    """Seerr answered a change with an error of its own: its message (a request that
    already exists, a quota), which says nothing secret"""


def seerr_get(url: str, apikey: str, path: str, timeout: float = TIMEOUT, **params: object) -> Any:
    response = requests.get(
        f"{url.rstrip('/')}/api/v1/{path}", params=params or None, headers={"X-Api-Key": apikey}, timeout=timeout
    )
    if response.status_code in (401, 403):
        raise SeerrRefused(f"Seerr refused the API key (HTTP {response.status_code})")
    response.raise_for_status()
    return response.json()


def seerr_post(url: str, apikey: str, path: str, body: dict[str, Any], timeout: float = TIMEOUT) -> Any:
    """a change, its answer's json. Seerr says why it won't (409 already requested, 403 a
    quota or a permission, 202 nothing left to request) in a message: SeerrDeclined"""
    response = requests.post(
        f"{url.rstrip('/')}/api/v1/{path}", json=body, headers={"X-Api-Key": apikey}, timeout=timeout
    )
    if response.status_code == 401:
        raise SeerrRefused("Seerr refused the API key (HTTP 401)")
    try:
        answer = response.json()
    except requests.exceptions.JSONDecodeError:
        answer = None
    if response.status_code in (200, 201):
        return answer
    message = answer.get("message") if isinstance(answer, dict) else None
    if response.status_code in (202, 400, 403, 404, 409, 422, 500) and isinstance(message, str) and message:
        raise SeerrDeclined(f"Seerr didn't request it: {message[:300]}")
    response.raise_for_status()
    raise SeerrDeclined(f"Seerr didn't request it (HTTP {response.status_code})")
