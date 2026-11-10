"""settings_routes.py - Settings pages: general, empty trash, the webhook key and the
config's history. Connections, rules and the json api have modules of their own"""

import copy
import difflib
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

import config
from config.models import WebDefaults
from web.db import ConfigVersion
from web.lookups import (
    LookupFailed,
)
from web.routes import loaded_settings
from web.settings_common import (
    checked_duration,
    checked_int,
    commit,
    dry_run_choice,
    first,
    FormLists,
    lines,
    lookups,
    page,
    set_dry_run_choice,
    store,
)
from web.webhooks import api_key, regenerate_api_key


logger = logging.getLogger(__name__)

router = APIRouter()

# config versions on a page of the history, older ones a page at a time
HISTORY_PAGE = 100


# ---------------------------------------------------------------------------
# general
# ---------------------------------------------------------------------------


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
        "dry_run": document.get("settings", {}).get("dry_run", True),
        "filter_time": document.get("settings", {}).get("filter_time", ""),
        "followup_delay": config.format_duration(web.followup_delay),
        "followup_attempts": web.followup_attempts,
        "followup_lookback": config.format_duration(web.followup_lookback),
        "plan_max_age": config.format_duration(web.plan_max_age),
        "min_free_gb": web.min_free_gb,
    }
    return page(request, "general", {"values": values}, errors)


@router.post("/settings")
def save_general(request: Request, fields: FormLists) -> Response:
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
    settings, error = loaded_settings(request)
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
            "global_dry": document.get("settings", {}).get("dry_run", True),
        },
        errors,
    )


@router.get("/settings/trash", response_class=HTMLResponse)
def trash_page(request: Request) -> HTMLResponse:
    return trash(request)


@router.post("/settings/trash")
def save_trash(request: Request, fields: FormLists) -> Response:
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
def version_route(request: Request, version_id: int) -> HTMLResponse:
    return version_page(request, version_id)


def version_page(request: Request, version_id: int, error: str | None = None) -> HTMLResponse:
    """a version and its diff, with a failed restore's error: never from the url, where
    anyone could put text in the page's error banner"""
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
def restore_version(request: Request, version_id: int, fields: FormLists) -> Response:
    version = store(request).version(version_id)
    if version is None:
        raise HTTPException(status_code=404)
    return commit(
        request, fields, copy.deepcopy(version.document), f"Restored version {version_id}",
        lambda e: version_page(request, version_id, e), "/settings/history",
    )
