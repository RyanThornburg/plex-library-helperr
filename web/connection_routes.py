"""connection_routes.py - Settings → Connections: each service's url and secret (Save,
Test, Disconnect) and the Status page's other links"""

import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from config.document import SECRET_FIELDS
from config.models import LINK_SERVICES, PlexHostConfig
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
# services with basic auth, whose username is optional
USERNAME_SERVICES = ("autoscan",)
# Plex's form also holds its machine's stats: node_exporter on the machine Plex runs on,
# for when that isn't Unraid ([plex_host], its own card on the status page)
HOST_HINT = (
    "Optional, for when Plex runs on its own machine: Prometheus node_exporter on that machine "
    "(on Ubuntu: sudo apt install prometheus-node-exporter) adds a Plex host card to the Status page with its "
    "media mounts, disk space, CPU, memory and network. Leave the URL empty to turn it off, and the username "
    "empty if node_exporter has no password."
)
HOST_SECRET = "plex_host.password"
# secrets a service can't work without (autoscan and node_exporter may have no password)
REQUIRED_SECRETS = ("radarr", "sonarr", "plex", "tautulli", "seerr", "sabnzbd", "bazarr", "unraid")
SERVICE_HINTS = {
    "radarr": "Settings → General → API Key in Radarr.",
    "sonarr": "Settings → General → API Key in Sonarr.",
    "plex": "Your X-Plex-Token (the server owner's). Used to refresh libraries, empty the trash, and for the Status page's checks of its trash, files and settings. Below it, optionally, the machine Plex runs on.",
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


def host_values(document: dict[str, Any]) -> dict[str, str]:
    """the Plex form's machine part"""
    section = document.get("plex_host", {})
    return {
        "host_url": section.get("url") or "",
        "host_username": section.get("username") or "",
        "host_mounts": "\n".join(section.get("mounts") or []),
        "host_min_free_gb": str(section.get("min_free_gb", PlexHostConfig.min_free_gb)),
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
    forms["plex"].update(host_values(document))
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
            "host_hint": HOST_HINT,
            "host_secret": HOST_SECRET,
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
    if service == "plex":
        values |= {key: first(fields, key) for key in ("host_url", "host_username", "host_mounts", "host_min_free_gb")}
        values["host_url"] = values["host_url"].strip()
    new_host_secret = fields.get("host_secret", [""])[-1].strip()
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
        removed: dict[str, str | None] = {secret_name: None}
        if service == "plex":
            document.pop("plex_host", None)
            removed[HOST_SECRET] = None
        lookups(request).clear()
        return commit(
            request, fields, document, f"{label} disconnected", failed,
            "/settings/connections", removed, check_live=False,
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
    host_mounts: list[str] = []
    host_min_free = PlexHostConfig.min_free_gb
    if service == "plex" and values["host_url"].strip():
        values["host_url"] = checked_url(values["host_url"], errors)
        host_mounts = [m.strip() for m in values["host_mounts"].splitlines() if m.strip()]
        errors += [f"Mounts must be absolute paths, e.g. /mnt/media: {m}" for m in host_mounts if not m.startswith("/")]
        try:
            host_min_free = int(values["host_min_free_gb"].strip() or PlexHostConfig.min_free_gb)
            if host_min_free < 0:
                raise ValueError
        except ValueError:
            errors.append("Low disk space below must be a whole number of GB, 0 or more")
    if errors:
        return connections(request, service, values, errors)

    if action == "test":
        try:
            secret = new_secret or stored.secret(secret_name)
            result = check_connection(service, {**values, "secret": secret})
        except (LookupFailed, SecretError) as e:
            return connections(request, service, values, [f"Test failed: {e}"])
        if service == "plex" and values["host_url"]:
            try:
                host_secret = new_host_secret or (stored.secret(HOST_SECRET) if HOST_SECRET in stored.secret_info() else "")
                machine = check_connection(
                    "plex_host",
                    {"url": values["host_url"], "username": values["host_username"].strip(), "secret": host_secret},
                )
            except (LookupFailed, SecretError) as e:
                return connections(request, service, values, [f"Connected to {result}, but its machine stats failed: {e}"])
            result += f", and its machine's stats: {machine}"
        return connections(request, service, values, tested=f"Connected to {result}")

    section = document.setdefault(service, {})
    section[url_key] = values["url"]
    if service in PUBLIC_URL_SERVICES and values["public_url"]:
        section["public_url"] = values["public_url"]
    else:
        section.pop("public_url", None)
    if service in USERNAME_SERVICES:
        if values["username"]:
            section["username"] = values["username"]
        else:
            section.pop("username", None)
    secrets: dict[str, str | None] = {secret_name: new_secret} if new_secret else {}
    if service == "plex":
        if values["host_url"]:
            host = {"url": values["host_url"]}
            if values["host_username"].strip():
                host["username"] = values["host_username"].strip()
            if host_mounts:
                host["mounts"] = host_mounts
            if host_min_free != PlexHostConfig.min_free_gb:
                host["min_free_gb"] = host_min_free
            document["plex_host"] = host
            if new_host_secret:
                secrets[HOST_SECRET] = new_host_secret
        else:
            # no url turns the machine's stats off, with their password
            document.pop("plex_host", None)
            secrets[HOST_SECRET] = None
    lookups(request).clear()
    return commit(
        request, fields, document, f"{label} connection",
        failed, "/settings/connections", secrets or None, check_live=False,
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
