"""unraid.py - Unraid's GraphQL api (/graphql, built into Unraid 7.2+, the Unraid Connect
plugin before that), for the status page and the connection Test. Read only: a key with
the Viewer role is enough. UnraidRefused's text is safe to show"""

from typing import Any

import requests

from logging_config import url_hosts

TIMEOUT = 10
# what unraid's api says when the key is wrong or can't read something
AUTH_CODES = ("UNAUTHENTICATED", "FORBIDDEN")


class UnraidRefused(Exception):
    """Unraid refused the key or the query"""


def unraid_query(url: str, apikey: str, query: str, timeout: float = TIMEOUT) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """the answer's data and its errors. A field the key can't read, or docker while it's
    stopped, is null with an error and the rest still answers; raises UnraidRefused when
    nothing answered"""
    response = requests.post(
        f"{url.rstrip('/')}/graphql",
        json={"query": query},
        headers={"x-api-key": apikey},
        timeout=timeout,
        # unraid with SSL redirects http to its https address, and a redirected POST
        # becomes a GET
        allow_redirects=False,
    )
    if 300 <= response.status_code < 400:
        where = url_hosts(str(response.headers.get("location") or ""))
        raise UnraidRefused(f"Unraid redirected to {where or 'another address'}: use that address as the URL")
    if response.status_code in (401, 403):
        raise UnraidRefused(f"Unraid refused the API key (HTTP {response.status_code})")
    try:
        body = response.json()
    except requests.exceptions.JSONDecodeError:
        response.raise_for_status()
        raise
    if not isinstance(body, dict):
        response.raise_for_status()
        raise UnraidRefused("Unraid answered something that isn't its API")
    errors = [e for e in body.get("errors") or [] if isinstance(e, dict)]
    data = body.get("data")
    if not isinstance(data, dict) or not any(v is not None for v in data.values()):
        if any((e.get("extensions") or {}).get("code") in AUTH_CODES for e in errors):
            raise UnraidRefused(f"Unraid refused the API key: {error_text(errors)}")
        if errors:
            raise UnraidRefused(f"Unraid answered: {error_text(errors)}")
        response.raise_for_status()
        raise UnraidRefused("Unraid answered without data")
    return data, errors


def error_text(errors: list[dict[str, Any]], field: str | None = None) -> str:
    """the first error's message, of those for `field` (the first part of its path) when
    given"""
    for error in errors:
        path = error.get("path") or []
        if field is None or (path and path[0] == field):
            return url_hosts(str(error.get("message") or "unknown error"))[:300]
    return "no answer"
