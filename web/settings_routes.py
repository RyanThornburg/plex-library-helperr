"""settings_routes.py - pages and json api for editing the config: general settings,
connections, radarr/sonarr rules, empty trash, the webhook key and the config's history"""

import copy
import difflib
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

import config
from config.document import RULE_TYPES, SECRET_FIELDS, Rule, rule_class, rule_to_dict
from config.models import REMOVER_TYPES, ConfigSettings, WebDefaults
from web.config_store import ConfigConflict, ConfigStore
from web.db import ConfigVersion
from web.lookups import (
    ArrLibrary,
    LookupFailed,
    Lookups,
    Preview,
    check_connection,
    preview_mover,
    preview_profiler,
)
from web.routes import error_page, templates
from web.secret_box import SecretError
from web.webhooks import api_key, regenerate_api_key

logger = logging.getLogger(__name__)

router = APIRouter()

ARRS = ("radarr", "sonarr")
# the media field each arr matches that the other doesn't
ARR_FIELD = {"radarr": "studios", "sonarr": "networks"}
RULE_NAMES = {"movers": "mover", "profilers": "profiler", "removers": "remover"}
# the jobs whose dry run settings live in the config, removers only delete approvals
DRY_RUN_JOBS = ("movers", "empty_trash")

# the config section of each: label, its secret's label, the url key
SERVICES: dict[str, tuple[str, str, str]] = {
    "radarr": ("Radarr", "API key", "baseurl"),
    "sonarr": ("Sonarr", "API key", "baseurl"),
    "plex": ("Plex", "Token", "baseurl"),
    "tautulli": ("Tautulli", "API key", "url"),
    "seerr": ("Seerr", "API key", "url"),
    "sabnzbd": ("SABnzbd", "API key", "url"),
    "bazarr": ("Bazarr", "API key", "url"),
    "autoscan": ("Autoscan", "Password", "url"),
}
SERVICE_SECRETS = {
    service: name for name, (service, _) in SECRET_FIELDS.items()
}
# the services the status page links to, which can have a public url too
PUBLIC_URL_SERVICES = ("radarr", "sonarr", "tautulli", "seerr", "sabnzbd", "bazarr")
# secrets a service can't work without (autoscan may have no password)
REQUIRED_SECRETS = ("radarr", "sonarr", "plex", "tautulli", "seerr", "sabnzbd", "bazarr")
SERVICE_HINTS = {
    "radarr": "Settings → General → API Key in Radarr.",
    "sonarr": "Settings → General → API Key in Sonarr.",
    "plex": "Your X-Plex-Token. Used to refresh libraries and empty the trash.",
    "tautulli": "Settings → Web Interface → API key. Watch history for removers' watch protection; without it, Plex history is used (completed plays only).",
    "seerr": "Settings → General → API Key in Seerr (or Overseerr). Requests and who made them, for the Requests page.",
    "sabnzbd": "Config → General → API Key in SABnzbd (not the NZB key). Queue, speed, warnings and failed downloads, for the Status page.",
    "bazarr": "Settings → General → API Key in Bazarr. Health, throttled providers and wanted subtitles, for the Status page. Include Bazarr's base URL if it has one (e.g. /subs).",
    "autoscan": "Moved folders go to Autoscan's manual trigger instead of refreshing whole Plex libraries. Leave the username empty if Autoscan has no auth.",
}

templates.env.globals["SERVICES"] = SERVICES


def store(request: Request) -> ConfigStore:
    return request.app.state.store


def lookups(request: Request) -> Lookups:
    return request.app.state.lookups


# config versions on a page of the history, older ones a page at a time
HISTORY_PAGE = 100
# rule forms hold lists (a profiler's titles), so more than the schedule form's cap
MAX_SETTINGS_FORM_BYTES = 256_000


async def form_lists(request: Request) -> dict[str, list[str]]:
    """a urlencoded form body with every value of repeated fields (checkboxes)"""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_SETTINGS_FORM_BYTES:
            raise HTTPException(status_code=413, detail="The form was too large to save.")
    return parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)


def first(fields: dict[str, list[str]], name: str) -> str:
    values = fields.get(name)
    return values[-1].strip() if values else ""


def lines(fields: dict[str, list[str]], name: str) -> list[str]:
    """a list field: one value per line (or repeated fields), blanks and repeats dropped"""
    values: list[str] = []
    for raw in fields.get(name, []):
        for value in raw.splitlines():
            value = value.strip()
            if value and value.casefold() not in {v.casefold() for v in values}:
                values.append(value)
    return values


def form_version(fields: dict[str, list[str]]) -> int | None:
    try:
        return int(first(fields, "version"))
    except ValueError:
        return None


def check_arr(arr: str) -> None:
    if arr not in ARRS:
        raise HTTPException(status_code=404)


def check_kind(arr: str, kind: str) -> None:
    check_arr(arr)
    if kind not in RULE_TYPES:
        raise HTTPException(status_code=404)


def current_settings(request: Request) -> tuple[ConfigSettings | None, str | None]:
    """the stored config with its secrets, for lookups. None and why when it can't load"""
    try:
        return store(request).load_settings(), None
    except (ValueError, SecretError) as e:
        return None, str(e)


def dry_run_states(document: dict[str, Any]) -> dict[str, bool | None]:
    settings = config.build_config(document)
    return {job: config.config_dry_run(job, settings) for job in DRY_RUN_JOBS}


def turns_live(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """jobs a change takes off dry run, they need confirming"""
    try:
        was = dry_run_states(before)
    except ValueError:
        was = dict.fromkeys(DRY_RUN_JOBS, True)
    now = dry_run_states(after)
    return [job for job in DRY_RUN_JOBS if was[job] is True and now[job] is not True]


def commit(
    request: Request,
    fields: dict[str, list[str]],
    document: dict[str, Any],
    note: str,
    on_error: Callable[[str], Response],
    redirect: str,
    secrets: dict[str, str | None] | None = None,
    check_live: bool = True,
) -> Response:
    """save a form's change, going through a confirmation when it makes a job live"""
    if check_live and not first(fields, "confirm_live"):
        try:
            live = turns_live(store(request).document()[1], document)
        except ValueError as e:
            return on_error(str(e))
        if live:
            return templates.TemplateResponse(
                request,
                "settings/confirm_live.html",
                {
                    "user": request.state.user,
                    "jobs": live,
                    "fields": [
                        (name, value) for name, values in fields.items() for value in values
                    ],
                    "action": request.url.path,
                    "back": redirect,
                },
            )
    try:
        store(request).save(
            document, request.state.user, note, form_version(fields), secrets
        )
    except (ConfigConflict, ValueError) as e:
        return on_error(str(e))
    return RedirectResponse(f"{redirect}{'&' if '?' in redirect else '?'}saved=1", status_code=303)


def page(request: Request, name: str, context: dict[str, Any], errors: list[str] | None = None) -> HTMLResponse:
    version, document = store(request).document()
    return templates.TemplateResponse(
        request,
        f"settings/{name}.html",
        {
            "user": request.state.user,
            "version": version,
            "document": document,
            "errors": errors or [],
            "saved": request.query_params.get("saved") == "1",
            **context,
        },
        status_code=400 if errors else 200,
    )


def dry_run_choice(section: dict[str, Any]) -> str:
    return "inherit" if "dry_run" not in section else ("true" if section["dry_run"] else "false")


def set_dry_run_choice(section: dict[str, Any], choice: str) -> None:
    if choice == "inherit":
        section.pop("dry_run", None)
    elif choice in ("true", "false"):
        section["dry_run"] = choice == "true"
    else:
        raise ValueError(f"unknown dry run choice '{choice}'")


def checked_duration(value: str, label: str, errors: list[str], optional: bool = False) -> str | None:
    if optional and not value:
        return None
    try:
        config.parse_duration(value, label)
    except ValueError as e:
        errors.append(str(e))
    return value


def checked_int(value: str, label: str, errors: list[str], minimum: int, optional: bool = False) -> int | None:
    if optional and not value:
        return None
    try:
        number = int(value)
    except ValueError:
        errors.append(f"{label} must be a whole number")
        return None
    if number < minimum:
        errors.append(f"{label} must be at least {minimum}")
    return number


# ---------------------------------------------------------------------------
# general
# ---------------------------------------------------------------------------


def toml_status(request: Request) -> dict[str, Any]:
    path = config.config_toml_path()
    versions = store(request).history(limit=1000)
    imported = next((v for v in reversed(versions) if v.note.startswith("Imported from")), None)
    return {
        "import_error": request.app.state.import_error,
        "imported": imported,
        "toml_exists": path.exists(),
        "toml_path": str(path),
    }


@router.get("/settings", response_class=HTMLResponse)
def general_page(request: Request) -> HTMLResponse:
    return general(request)


def general(request: Request, values: dict[str, Any] | None = None, errors: list[str] | None = None) -> HTMLResponse:
    _, document = store(request).document()
    web = None
    if values is None:
        # this is the page to fix an invalid config on, it shows with the defaults
        try:
            web = config.build_config(document).web
        except ValueError as e:
            web, errors = WebDefaults(), [*(errors or []), f"The saved settings are invalid: {e}"]
    values = values or {
        "dry_run": document.get("settings", {}).get("dry_run", False),
        "filter_time": document.get("settings", {}).get("filter_time", ""),
        "followup_delay": config.format_duration(web.followup_delay),
        "followup_attempts": web.followup_attempts,
        "followup_lookback": config.format_duration(web.followup_lookback),
        "plan_max_age": config.format_duration(web.plan_max_age),
        "min_free_gb": web.min_free_gb,
    }
    return page(request, "general", {"values": values, **toml_status(request)}, errors)


@router.post("/settings")
async def save_general(request: Request) -> Response:
    fields = await form_lists(request)
    errors: list[str] = []
    values = {
        "dry_run": "dry_run" in fields,
        "filter_time": first(fields, "filter_time"),
        "followup_delay": first(fields, "followup_delay"),
        "followup_attempts": first(fields, "followup_attempts"),
        "followup_lookback": first(fields, "followup_lookback"),
        "plan_max_age": first(fields, "plan_max_age"),
        "min_free_gb": first(fields, "min_free_gb"),
    }
    checked_duration(values["filter_time"], "Only media added within", errors, optional=True)
    checked_duration(values["followup_delay"], "Follow-up delay", errors)
    attempts = checked_int(values["followup_attempts"], "Follow-up attempts", errors, 0)
    checked_duration(values["followup_lookback"], "Follow-up look back", errors)
    checked_duration(values["plan_max_age"], "Plan max age", errors)
    min_free_gb = checked_int(values["min_free_gb"], "Low disk space", errors, 0)
    if errors:
        return general(request, values, errors)

    _, document = store(request).document()
    settings = document.setdefault("settings", {})
    settings["dry_run"] = values["dry_run"]
    if values["filter_time"]:
        settings["filter_time"] = values["filter_time"]
    else:
        settings.pop("filter_time", None)
    document["web"] = {
        "followup_delay": values["followup_delay"],
        "followup_attempts": attempts,
        "followup_lookback": values["followup_lookback"],
        "plan_max_age": values["plan_max_age"],
        "min_free_gb": min_free_gb,
    }
    return commit(
        request,
        fields,
        document,
        "General settings",
        lambda e: general(request, values, [e]),
        "/settings",
    )


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
async def save_connection(request: Request, service: str) -> Response:
    if service not in SERVICES:
        raise HTTPException(status_code=404)
    fields = await form_lists(request)
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
            secret = new_secret or stored.secrets().get(secret_name, "")
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
async def save_links(request: Request) -> Response:
    """other tools the status page links to: add one, or remove one by its number"""
    fields = await form_lists(request)
    action = first(fields, "action")
    values = {key: first(fields, key).strip().rstrip("/") for key in ("name", "url", "public_url")}
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


# ---------------------------------------------------------------------------
# empty trash
# ---------------------------------------------------------------------------


def trash(request: Request, values: dict[str, Any] | None = None, errors: list[str] | None = None) -> HTMLResponse:
    _, document = store(request).document()
    section = document.get("plex", {}).get("empty_trash", {})
    defaults = config.build_config({}).empty_trash
    values = values or {
        "libraries": section.get("libraries", []),
        "max_items": section.get("max_items", defaults.max_items),
        "max_deleted_items": section.get("max_deleted_items", defaults.max_deleted_items),
        "dry_run": dry_run_choice(section),
    }
    settings, error = current_settings(request)
    plex_libraries: list[str] = []
    problems = [] if settings else [f"Libraries couldn't load: {error}"]
    if settings:
        try:
            plex_libraries = [
                title for title, kind in lookups(request).plex_libraries(settings.plex)
                if kind in ("movie", "show")
            ]
        except LookupFailed as e:
            problems.append(str(e))
    return page(
        request,
        "trash",
        {
            "values": values,
            "plex_libraries": plex_libraries,
            "choice_problems": problems,
            "global_dry": document.get("settings", {}).get("dry_run", False),
        },
        errors,
    )


@router.get("/settings/trash", response_class=HTMLResponse)
def trash_page(request: Request) -> HTMLResponse:
    return trash(request)


@router.post("/settings/trash")
async def save_trash(request: Request) -> Response:
    fields = await form_lists(request)
    errors: list[str] = []
    values = {
        "libraries": lines(fields, "libraries"),
        "max_items": first(fields, "max_items"),
        "max_deleted_items": first(fields, "max_deleted_items"),
        "dry_run": first(fields, "dry_run"),
    }
    max_items = checked_int(values["max_items"], "Max items", errors, 1)
    max_deleted = checked_int(values["max_deleted_items"], "Max fully deleted items", errors, 0)
    if errors:
        return trash(request, values, errors)
    _, document = store(request).document()
    section: dict[str, Any] = {"max_items": max_items, "max_deleted_items": max_deleted}
    if values["libraries"]:
        section["libraries"] = values["libraries"]
    try:
        set_dry_run_choice(section, values["dry_run"])
    except ValueError as e:
        return trash(request, values, [str(e)])
    document.setdefault("plex", {})["empty_trash"] = section
    return commit(
        request, fields, document, "Empty trash settings",
        lambda e: trash(request, values, [e]), "/settings/trash",
    )


# ---------------------------------------------------------------------------
# webhook
# ---------------------------------------------------------------------------


@router.get("/settings/webhook", response_class=HTMLResponse)
def webhook_page(request: Request) -> HTMLResponse:
    return page(
        request,
        "webhook",
        {
            "api_key": api_key(request.app.state.engine),
            "port": os.environ.get("PORT", "8080"),
        },
    )


@router.post("/settings/api-key")
def regenerate_api_key_form(request: Request) -> RedirectResponse:
    regenerate_api_key(request.app.state.engine, request.state.user)
    return RedirectResponse("/settings/webhook", status_code=303)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def document_lines(version: ConfigVersion | None) -> list[str]:
    if version is None:
        return []
    return json.dumps(version.document, indent=2, sort_keys=True).splitlines()


def diff(before: ConfigVersion | None, after: ConfigVersion) -> list[tuple[str, str]]:
    """(css class, line) of a unified diff between two versions' documents"""
    result = []
    for line in difflib.unified_diff(
        document_lines(before), document_lines(after), lineterm="", n=3
    ):
        if line.startswith(("---", "+++")):
            continue
        kind = {"+": "add", "-": "del", "@": "hunk"}.get(line[:1], "")
        result.append((kind, line))
    return result


@router.get("/settings/history", response_class=HTMLResponse)
def history_page(request: Request) -> HTMLResponse:
    before = request.query_params.get("before", "")
    before_id = int(before) if before.isdigit() and len(before) < 18 else None
    versions = store(request).history(HISTORY_PAGE + 1, before_id)
    return page(
        request, "history",
        {"versions": versions[:HISTORY_PAGE], "older": len(versions) > HISTORY_PAGE, "before": before_id},
    )


@router.get("/settings/history/{version_id}", response_class=HTMLResponse)
def version_page(request: Request, version_id: int, error: str | None = None) -> HTMLResponse:
    version = store(request).version(version_id)
    if version is None:
        raise HTTPException(status_code=404)
    previous = store(request).previous(version_id)
    current_id, _ = store(request).document()
    current = store(request).version(current_id) if current_id else None
    return page(
        request,
        "version",
        {
            "shown": version,
            "previous": previous,
            "changes": diff(previous, version),
            # what restoring would change
            "restore_changes": diff(current, version) if current and current.id != version.id else [],
            "is_current": current_id == version.id,
        },
        [error] if error else None,
    )


@router.post("/settings/history/{version_id}/restore")
async def restore_version(request: Request, version_id: int) -> Response:
    fields = await form_lists(request)
    version = store(request).version(version_id)
    if version is None:
        raise HTTPException(status_code=404)
    return commit(
        request, fields, copy.deepcopy(version.document), f"Restored version {version_id}",
        lambda e: version_page(request, version_id, e), "/settings/history",
    )


# ---------------------------------------------------------------------------
# radarr / sonarr rules. Registered after the other /settings pages: routes match in
# order, and /settings/{arr} would catch /settings/trash
# ---------------------------------------------------------------------------


def criteria(kind: str, rule: dict[str, Any]) -> list[str]:
    """a rule's conditions in words, for the rules tables"""
    def join(name: str) -> str:
        return ", ".join(rule.get(name, []))

    parts: list[str] = []
    if kind == "movers":
        if rule.get("genres"):
            parts.append(f"genres {rule.get('genre_match', 'any')} of {join('genres')}")
        if rule.get("tags"):
            parts.append(f"tags {rule.get('tag_match', 'any')} of {join('tags')}")
        for name in ("certifications", "studios", "networks"):
            if rule.get(name):
                parts.append(f"{name} {join(name)}")
        for name in ("exclude_genres", "exclude_tags", "exclude_certifications", "exclude_paths"):
            if rule.get(name):
                parts.append(f"not {name.removeprefix('exclude_')} {join(name)}")
    elif kind == "profilers":
        for name in ("titles", "studios", "networks"):
            if rule.get(name):
                parts.append(f"{name} {join(name)}")
        if "monitored" in rule:
            parts.append("monitor" if rule["monitored"] else "unmonitor")
    return parts


templates.env.globals["criteria"] = criteria


@router.get("/settings/{arr}", response_class=HTMLResponse)
def arr_page(request: Request, arr: str) -> HTMLResponse:
    check_arr(arr)
    return arr_settings(request, arr)


def arr_settings(
    request: Request, arr: str, values: dict[str, str] | None = None, errors: list[str] | None = None
) -> HTMLResponse:
    _, document = store(request).document()
    section = document.get(arr, {})
    values = values or {"dry_run": dry_run_choice(section), "tag": section.get("tag", "")}
    global_dry = document.get("settings", {}).get("dry_run", False)
    return page(
        request,
        "arr",
        {
            "arr": arr,
            "label": SERVICES[arr][0],
            "section": section,
            "values": values,
            "global_dry": global_dry,
            "connected": bool(section.get("baseurl")),
        },
        errors,
    )


@router.post("/settings/{arr}")
async def save_arr(request: Request, arr: str) -> Response:
    check_arr(arr)
    fields = await form_lists(request)
    values = {"dry_run": first(fields, "dry_run"), "tag": first(fields, "tag")}
    _, document = store(request).document()
    section = document.setdefault(arr, {})
    try:
        set_dry_run_choice(section, values["dry_run"])
    except ValueError as e:
        return arr_settings(request, arr, values, [str(e)])
    if values["tag"]:
        section["tag"] = values["tag"]
    else:
        section.pop("tag", None)
    return commit(
        request, fields, document, f"{SERVICES[arr][0]} options",
        lambda e: arr_settings(request, arr, values, [e]), f"/settings/{arr}",
    )


def back_url(arr: str, kind: str) -> str:
    return "/removers" if kind == "removers" else f"/settings/{arr}"


def rule_number(document: dict[str, Any], arr: str, kind: str, number: int) -> dict[str, Any]:
    rules = document.get(arr, {}).get(kind, [])
    if not 1 <= number <= len(rules):
        raise HTTPException(status_code=404)
    return rules[number - 1]


def choices(request: Request, arr: str, refresh: bool = False) -> tuple[ArrLibrary | None, list[str], list[str]]:
    """(the arr's library, plex libraries, problems loading them) for a rule form"""
    settings, error = current_settings(request)
    if settings is None:
        return None, [], [f"Choices couldn't load: {error}"]
    problems = []
    library = None
    try:
        library = lookups(request).arr_library(arr, getattr(settings, arr), refresh)
    except LookupFailed as e:
        problems.append(str(e))
    plex: list[str] = []
    try:
        plex = [title for title, _ in lookups(request).plex_libraries(settings.plex)]
    except LookupFailed as e:
        problems.append(str(e))
    return library, plex, problems


def fingerprint(rule: Any) -> str:
    """a stored rule as it was when its form loaded: rules are saved by number, which
    another tab deleting or moving one changes"""
    return hashlib.sha256(json.dumps(rule, sort_keys=True).encode()).hexdigest()[:16]


def changed_since_loaded(document: dict[str, Any], arr: str, kind: str, number: int, fields: dict[str, list[str]]) -> bool:
    loaded = first(fields, "loaded")
    return bool(loaded) and loaded != fingerprint(rule_number(document, arr, kind, number))


RULE_CHANGED = (
    "{} changed since this form was opened (in another tab, or by a restore), so this wasn't saved "
    "to keep from overwriting the wrong rule. Open it again from the list."
)


def rule_form(
    request: Request,
    arr: str,
    kind: str,
    number: int | None,
    values: dict[str, Any],
    errors: list[str] | None = None,
    preview: Preview | None = None,
    loaded: str | None = None,
) -> HTMLResponse:
    if number is not None and loaded is None:
        _, document = store(request).document()
        loaded = fingerprint(rule_number(document, arr, kind, number))
    library, plex_libraries, problems = choices(
        request, arr, request.query_params.get("refresh") == "1"
    )
    settings, _ = current_settings(request)
    # radarr removers remove movies by root folder, a form of their own
    form = "movie_remover" if (arr, kind) == ("radarr", "removers") else RULE_NAMES[kind]
    return page(
        request,
        f"{form}_form",
        {
            "arr": arr,
            "label": SERVICES[arr][0],
            "kind": kind,
            "number": number,
            "values": values,
            "library": library,
            "plex_libraries": plex_libraries,
            "choice_problems": problems,
            "arr_field": ARR_FIELD[arr],
            "preview": preview,
            "filter_time": settings.filter_time if settings else None,
            "back": back_url(arr, kind),
            "remover_types": REMOVER_TYPES,
            "loaded": loaded,
        },
        errors,
    )


def rule_values(kind: str, arr: str, fields: dict[str, list[str]]) -> dict[str, Any]:
    """a rule form's fields, as the rule's document form (not validated yet)"""
    lists = {
        "movers": ["exclude_paths", "certifications", "exclude_certifications", "genres",
                   "exclude_genres", ARR_FIELD[arr], "tags", "exclude_tags"],
        "profilers": ["titles", ARR_FIELD[arr]],
        "removers": ["exclude_tags"] if arr == "radarr" else ["tags", "exclude_tags"],
    }[kind]
    values: dict[str, Any] = {name: lines(fields, name) for name in lists}
    if kind == "movers":
        values.update(
            path=first(fields, "path"),
            plex_library=first(fields, "plex_library") or None,
            genre_match=first(fields, "genre_match") or "any",
            tag_match=first(fields, "tag_match") or "any",
        )
    elif kind == "profilers":
        monitored = first(fields, "monitored")
        values.update(
            profile=first(fields, "profile"),
            monitored=None if monitored == "" else monitored == "true",
        )
    elif arr == "radarr":
        # always, ever or within, kept so the form shows it again after an error
        watched = first(fields, "watched") or "always"
        values.update(
            path=first(fields, "path"),
            min_age=first(fields, "min_age"),
            watched=watched,
            skip_if_ever_watched=watched == "ever",
            skip_if_watched_within=(
                first(fields, "skip_if_watched_within") or None if watched == "within" else None
            ),
            plex_library=first(fields, "plex_library") or None,
        )
    else:
        values.update(
            type=first(fields, "type"),
            keep=first(fields, "keep"),
            min_age=first(fields, "min_age"),
            skip_if_watched_within=first(fields, "skip_if_watched_within") or None,
            include_specials="include_specials" in fields,
            plex_library=first(fields, "plex_library") or None,
        )
    return values


def build_rule(arr: str, kind: str, values: dict[str, Any]) -> Rule:
    """the rule from a form's values, ValueError saying what's wrong"""
    data = dict(values)
    if kind == "movers" and not data["path"]:
        raise ValueError("Move to is required")
    if kind == "profilers" and not data["profile"]:
        raise ValueError("Quality profile is required")
    if kind == "removers" and arr == "radarr":
        if not data["path"]:
            raise ValueError("Root folder is required")
        if data.pop("watched") == "within" and not data["skip_if_watched_within"]:
            raise ValueError("Played within is required to keep movies played recently")
    if kind == "removers":
        errors: list[str] = []
        if arr == "sonarr":
            data["keep"] = checked_int(data["keep"], "Keep", errors, 1)
        data["min_age"] = checked_duration(data["min_age"], "Older than", errors)
        if data["skip_if_watched_within"]:
            checked_duration(data["skip_if_watched_within"], "Watched within", errors)
        if errors:
            raise ValueError("; ".join(errors))
        data["min_age"] = config.parse_duration(data["min_age"], "Older than")
        if data["skip_if_watched_within"]:
            data["skip_if_watched_within"] = config.parse_duration(
                data["skip_if_watched_within"], "Watched within"
            )
    return rule_class(arr, kind)(**data)


@router.get("/settings/{arr}/{kind}/new", response_class=HTMLResponse)
def new_rule_page(request: Request, arr: str, kind: str) -> HTMLResponse:
    check_kind(arr, kind)
    return rule_form(request, arr, kind, None, {})


@router.get("/settings/{arr}/{kind}/{number}", response_class=HTMLResponse)
def edit_rule_page(request: Request, arr: str, kind: str, number: int) -> HTMLResponse:
    check_kind(arr, kind)
    _, document = store(request).document()
    return rule_form(request, arr, kind, number, rule_number(document, arr, kind, number))


@router.post("/settings/{arr}/{kind}/new")
async def create_rule(request: Request, arr: str, kind: str) -> Response:
    check_kind(arr, kind)
    return await save_rule(request, arr, kind, None)


@router.post("/settings/{arr}/{kind}/{number}")
async def update_rule(request: Request, arr: str, kind: str, number: int) -> Response:
    check_kind(arr, kind)
    return await save_rule(request, arr, kind, number)


async def save_rule(request: Request, arr: str, kind: str, number: int | None) -> Response:
    fields = await form_lists(request)
    values = rule_values(kind, arr, fields)
    # a re-rendered form keeps the rule it was opened on
    loaded = first(fields, "loaded") or None

    def failed(message: str) -> HTMLResponse:
        return rule_form(request, arr, kind, number, values, [message], loaded=loaded)

    try:
        rule = build_rule(arr, kind, values)
    except (TypeError, ValueError) as e:
        return failed(str(e))

    _, document = store(request).document()
    rules = document.setdefault(arr, {}).setdefault(kind, [])
    name = f"{SERVICES[arr][0]} {RULE_NAMES[kind]}"
    if number is not None and changed_since_loaded(document, arr, kind, number, fields):
        return failed(RULE_CHANGED.format(f"{name} {number}"))

    if first(fields, "action") == "preview" and kind != "removers":
        return rule_form(
            request, arr, kind, number, values,
            preview=rule_preview(request, arr, kind, number, rule, rules), loaded=loaded,
        )

    if number is None:
        rules.append(rule_to_dict(rule))
        note = f"{name} {len(rules)} added"
    else:
        rules[number - 1] = rule_to_dict(rule)
        note = f"{name} {number} edited"
    return commit(request, fields, document, note, failed, back_url(arr, kind), check_live=False)


def rule_preview(
    request: Request, arr: str, kind: str, number: int | None, rule: Any, rules: list[dict[str, Any]]
) -> Preview:
    settings, error = current_settings(request)
    if settings is None:
        return Preview(problem=error)
    try:
        library = lookups(request).arr_library(arr, getattr(settings, arr))
    except LookupFailed as e:
        return Preview(problem=str(e))
    log_name = arr.upper()
    if kind == "profilers":
        return preview_profiler(library, rule, settings.filter_time, log_name)
    arr_config = getattr(settings, arr)
    earlier = arr_config.movers[: number - 1] if number else arr_config.movers
    return preview_mover(library, earlier, rule, settings.filter_time, log_name)


@router.post("/settings/{arr}/{kind}/{number}/delete")
async def delete_rule(request: Request, arr: str, kind: str, number: int) -> Response:
    check_kind(arr, kind)
    fields = await form_lists(request)
    _, document = store(request).document()
    if changed_since_loaded(document, arr, kind, number, fields):
        return error_page(
            request, 409, RULE_CHANGED.format(f"{SERVICES[arr][0]} {RULE_NAMES[kind]} {number}"),
            ("Removers", "/removers") if kind == "removers" else (SERVICES[arr][0], f"/settings/{arr}"),
        )
    del document[arr][kind][number - 1]
    return commit(
        request, fields, document,
        f"{SERVICES[arr][0]} {RULE_NAMES[kind]} {number} deleted",
        lambda e: arr_settings(request, arr, errors=[e]) if kind != "removers" else error_page(request, 400, e, ("Removers", "/removers")),
        back_url(arr, kind), check_live=False,
    )


@router.post("/settings/{arr}/{kind}/{number}/move")
async def move_rule(request: Request, arr: str, kind: str, number: int, direction: str) -> Response:
    """movers run in order and the first match wins, so their order matters"""
    check_kind(arr, kind)
    fields = await form_lists(request)
    _, document = store(request).document()
    rule_number(document, arr, kind, number)
    rules = document[arr][kind]
    other = number - 1 if direction == "up" else number + 1
    if direction not in ("up", "down") or not 1 <= other <= len(rules):
        raise HTTPException(status_code=422, detail="A rule can only move up or down, within the list.")
    rules[number - 1], rules[other - 1] = rules[other - 1], rules[number - 1]
    return commit(
        request, fields, document,
        f"{SERVICES[arr][0]} {RULE_NAMES[kind]} {number} moved {direction}",
        lambda e: arr_settings(request, arr, errors=[e]),
        back_url(arr, kind), check_live=False,
    )


@router.post("/settings/{arr}/remover-settings")
async def save_remover_settings(request: Request, arr: str) -> Response:
    check_arr(arr)
    fields = await form_lists(request)
    errors: list[str] = []
    max_deletes = checked_int(first(fields, "max_deletes"), "Max deletes", errors, 1, optional=True)
    if errors:
        return error_page(request, 400, errors[0], ("Removers", "/removers"))
    _, document = store(request).document()
    section = document.setdefault(arr, {})
    if max_deletes is None:
        section.pop("remover_settings", None)
    else:
        section["remover_settings"] = {"max_deletes": max_deletes}
    return commit(
        request, fields, document, f"{SERVICES[arr][0]} remover max deletes",
        lambda e: error_page(request, 400, e, ("Removers", "/removers")), "/removers", check_live=False,
    )


# ---------------------------------------------------------------------------
# json api
# ---------------------------------------------------------------------------


def version_to_dict(version: ConfigVersion, include_document: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": version.id,
        "note": version.note,
        "created_at": version.created_at.isoformat() + "Z",
        "created_by": version.created_by,
    }
    if include_document:
        data["config"] = version.document
    return data


@router.get("/api/config")
def api_config(request: Request) -> dict[str, Any]:
    """the config without its secrets, which are only listed"""
    version, document = store(request).document()
    return {
        "version": version,
        "config": document,
        "secrets": {
            name: {"updated_at": info.updated_at.isoformat() + "Z", "readable": info.readable}
            for name, info in store(request).secret_info().items()
        },
    }


class ConfigRequest(BaseModel):
    # the version this change is based on, 0 before any
    version: int
    config: dict[str, Any]
    # turning a job live has to be asked for
    confirm_live: bool = False


def api_commit(request: Request, body: ConfigRequest, note: str) -> JSONResponse:
    current, document = store(request).document()
    if body.version != current:
        return JSONResponse(
            {"error": f"the config changed since version {body.version}, it's {current}"},
            status_code=409,
        )
    if not body.confirm_live:
        try:
            live = turns_live(document, body.config)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        if live:
            return JSONResponse(
                {"error": "this makes jobs live, send confirm_live: true", "live": live},
                status_code=422,
            )
    try:
        version = store(request).save(body.config, request.state.user, note, body.version)
    except ConfigConflict as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    return JSONResponse({"version": version})


@router.put("/api/config")
def api_save_config(request: Request, body: ConfigRequest) -> JSONResponse:
    """replace the config, secrets aren't part of it and stay as they are"""
    if secrets := [
        n for n, (s, k) in SECRET_FIELDS.items() if isinstance(body.config.get(s), dict) and k in body.config[s]
    ]:
        return JSONResponse(
            {"error": f"secrets can only be set on Settings → Connections: {secrets}"},
            status_code=422,
        )
    return api_commit(request, body, "Saved through the api")


@router.get("/api/config/versions")
def api_versions(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    return [version_to_dict(v) for v in store(request).history(max(1, min(limit, 500)))]


@router.get("/api/config/versions/{version_id}")
def api_version(request: Request, version_id: int) -> dict[str, Any]:
    version = store(request).version(version_id)
    if version is None:
        raise HTTPException(status_code=404)
    return version_to_dict(version, include_document=True)


class RestoreRequest(BaseModel):
    version: int
    confirm_live: bool = False


@router.post("/api/config/versions/{version_id}/restore")
def api_restore(request: Request, version_id: int, body: RestoreRequest) -> JSONResponse:
    version = store(request).version(version_id)
    if version is None:
        raise HTTPException(status_code=404)
    return api_commit(
        request,
        ConfigRequest(version=body.version, config=version.document, confirm_live=body.confirm_live),
        f"Restored version {version_id}",
    )

