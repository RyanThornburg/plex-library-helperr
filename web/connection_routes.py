"""connection_routes.py - Settings → Connections: each service's url and secret (Save,
Test, Disconnect) and the Status page's other links"""

import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from config.document import SECRET_FIELDS
from config.models import LINK_SERVICES
from web.lookups import (
    LookupFailed,
    check_connection,
)
from web.routes import templates
from web.secret_box import SecretError
from web.settings_common import (
    ARRS,
    SERVICES,
    commit,
    first,
    FormLists,
    lookups,
    page,
    store,
)


logger = logging.getLogger(__name__)

router = APIRouter()

SERVICE_SECRETS = {
    service: name for name, (service, _) in SECRET_FIELDS.items()
}
# the services the status page links to, which can have a public url too
PUBLIC_URL_SERVICES = ("radarr", "sonarr", "tautulli", "seerr", "sabnzbd", "bazarr", "unraid")
# secrets a service can't work without (autoscan may have no password)
REQUIRED_SECRETS = ("radarr", "sonarr", "plex", "tautulli", "seerr", "sabnzbd", "bazarr", "unraid")
SERVICE_HINTS = {
    "radarr": "Settings → General → API Key in Radarr.",
    "sonarr": "Settings → General → API Key in Sonarr.",
    "plex": "Your X-Plex-Token. Used to refresh libraries and empty the trash.",
    "tautulli": "Settings → Web Interface → API key. Watch history for removers' watch protection; without it, Plex history is used (completed plays only).",
    "seerr": "Settings → General → API Key in Seerr (or Overseerr). Requests and who made them, for the Requests page.",
    "sabnzbd": "Config → General → API Key in SABnzbd (not the NZB key). Queue, speed, warnings and failed downloads, for the Status page.",
    "bazarr": "Settings → General → API Key in Bazarr. Health, throttled providers and wanted subtitles, for the Status page. Include Bazarr's base URL if it has one (e.g. /subs).",
    "unraid": "Settings → Management Access → API Keys in Unraid 7.2 or later (the Unraid Connect plugin before that): create a key with the Viewer role, which can only read. The array, disks, parity checks, containers and notifications, for the Status page. The URL is the one you open Unraid's web UI at.",
    "autoscan": "Moved folders go to Autoscan's manual trigger instead of refreshing whole Plex libraries. Leave the username empty if Autoscan has no auth.",
}

templates.env.globals["SERVICES"] = SERVICES



# ---------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------


def service_values(document: dict[str, Any], service: str) -> dict[str, str]:
    _, _, url_key = SERVICES[service]
    section = document.get(service, {})
    return {
        "url": section.get(url_key) or "",
        "public_url": section.get("public_url") or "",
        "username": section.get("username") or "",
    }


def connections(
    request: Request,
    service: str | None = None,
    values: dict[str, str] | None = None,
    errors: list[str] | None = None,
    tested: str | None = None,
) -> HTMLResponse:
    """values, errors and the test result belong to `service`'s form"""
    _, document = store(request).document()
    forms = {s: service_values(document, s) for s in SERVICES}
    if service and values is not None:
        forms[service] = values
    response = page(
        request,
        "connections",
        {
            "forms": forms,
            "secrets": store(request).secret_info(),
            "service_secrets": SERVICE_SECRETS,
            "hints": SERVICE_HINTS,
            "public_url_services": PUBLIC_URL_SERVICES,
            "other_links": document.get("links", []),
            "link_services": LINK_SERVICES,
            "active": service,
            "service_errors": errors or [],
            "tested": tested,
        },
    )
    if errors:
        response.status_code = 400
    return response


@router.get("/settings/connections", response_class=HTMLResponse)
def connections_page(request: Request) -> HTMLResponse:
    return connections(request)


def checked_url(url: str, errors: list[str]) -> str:
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", url):
        errors.append("URL must start with http:// or https://, e.g. http://192.168.1.15:7878")
    return url


@router.post("/settings/connections/{service}")
def save_connection(request: Request, service: str, fields: FormLists) -> Response:
    if service not in SERVICES:
        raise HTTPException(status_code=404)
    action = first(fields, "action")
    label, secret_label, url_key = SERVICES[service]
    secret_name = SERVICE_SECRETS[service]
    stored = store(request)
    values = {
        "url": first(fields, "url"),
        "public_url": first(fields, "public_url"),
        "username": first(fields, "username"),
    }
    # the key field is never filled in by the page, empty keeps the saved one
    new_secret = fields.get("secret", [""])[-1].strip()

    def failed(message: str) -> HTMLResponse:
        return connections(request, service, values, [message])

    _, document = stored.document()
    if action == "disconnect":
        if service in ARRS or service == "plex":
            document.get(service, {}).pop(url_key, None)
            document.get(service, {}).pop("public_url", None)
        else:
            document.pop(service, None)
        lookups(request).clear()
        return commit(
            request, fields, document, f"{label} disconnected", failed,
            "/settings/connections", {secret_name: None}, check_live=False,
        )

    errors: list[str] = []
    values["url"] = checked_url(values["url"], errors)
    if values["public_url"]:
        values["public_url"] = values["public_url"].strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", values["public_url"]):
            errors.append("Public URL must start with http:// or https://, e.g. https://radarr.example.com")
    has_secret = bool(new_secret) or secret_name in stored.secret_info()
    if service in REQUIRED_SECRETS and not has_secret:
        errors.append(f"{secret_label} is required")
    if errors:
        return connections(request, service, values, errors)

    if action == "test":
        try:
            secret = new_secret or stored.secret(secret_name)
            result = check_connection(service, {**values, "secret": secret})
        except (LookupFailed, SecretError) as e:
            return connections(request, service, values, [f"Test failed: {e}"])
        return connections(request, service, values, tested=f"Connected to {result}")

    section = document.setdefault(service, {})
    section[url_key] = values["url"]
    if service in PUBLIC_URL_SERVICES and values["public_url"]:
        section["public_url"] = values["public_url"]
    else:
        section.pop("public_url", None)
    if service == "autoscan":
        if values["username"]:
            section["username"] = values["username"]
        else:
            section.pop("username", None)
    lookups(request).clear()
    return commit(
        request, fields, document, f"{label} connection",
        failed, "/settings/connections",
        {secret_name: new_secret} if new_secret else None, check_live=False,
    )


@router.post("/settings/links")
def save_links(request: Request, fields: FormLists) -> Response:
    """other tools the status page links to: add one, or remove one by its number"""
    action = first(fields, "action")
    values = {key: first(fields, key).strip().rstrip("/") for key in ("name", "url", "public_url", "service")}
    _, document = store(request).document()
    links = document.setdefault("links", [])

    def failed(message: str) -> HTMLResponse:
        return connections(request, "links", values, [message])

    if action == "remove":
        index = first(fields, "index")
        if not index.isdigit() or not 1 <= int(index) <= len(links):
            raise HTTPException(status_code=404)
        note = f"Link {links.pop(int(index) - 1).get('name')} removed"
    elif action == "add":
        links.append({key: value for key, value in values.items() if value})
        note = f"Link {values['name'] or '(no name)'} added"
    else:
        raise HTTPException(status_code=422)
    if not links:
        document.pop("links")
    return commit(request, fields, document, note, failed, "/settings/connections#links", check_live=False)
