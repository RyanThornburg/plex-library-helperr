"""settings_common.py - what the settings pages share: the config store, form parsing,
the live-change check every save goes through (save_checked), the page layout and the
settings each request loads once"""

import logging
from collections.abc import Callable
from typing import Annotated, Any
from urllib.parse import parse_qs

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

import config
from config.document import RULE_TYPES
from web.config_store import ConfigConflict, ConfigStore
from web.lookups import (
    Lookups,
)
from web.routes import templates


logger = logging.getLogger(__name__)

ARRS = ("radarr", "sonarr")
# the media field each arr matches that the other doesn't
ARR_FIELD = {"radarr": "studios", "sonarr": "networks"}
RULE_NAMES = {"movers": "mover", "profilers": "profiler", "removers": "remover"}
# the jobs whose dry run settings live in the config (removers only delete approvals),
# and the live_parts() each covers
DRY_RUN_JOBS: dict[str, set[str]] = {
    "movers": set(ARRS),
    "empty_trash": {"empty_trash"},
    "clear_stuck": {f"{arr}:queue" for arr in ARRS},
}

# the config section of each: label, its secret's label, the url key
SERVICES: dict[str, tuple[str, str, str]] = {
    "radarr": ("Radarr", "API key", "baseurl"),
    "sonarr": ("Sonarr", "API key", "baseurl"),
    "plex": ("Plex", "Token", "baseurl"),
    "tautulli": ("Tautulli", "API key", "url"),
    "seerr": ("Seerr", "API key", "url"),
    "sabnzbd": ("SABnzbd", "API key", "url"),
    "bazarr": ("Bazarr", "API key", "url"),
    "unraid": ("Unraid", "API key", "url"),
    "autoscan": ("Autoscan", "Password", "url"),
}
def store(request: Request) -> ConfigStore:
    return request.app.state.store


def lookups(request: Request) -> Lookups:
    return request.app.state.lookups


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


# handlers that save a form take it as a dependency: it's read on the event loop, and
# the handler itself is sync, so fastapi runs it (Test, Preview, sqlite writes) in a
# worker thread instead of freezing every other request while it waits
FormLists = Annotated[dict[str, list[str]], Depends(form_lists)]


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


def live_parts(document: dict[str, Any]) -> set[str]:
    """the parts of the config that change media as configured: an arr's movers by the
    arr's name, its queue cleanup as <arr>:queue, and empty_trash"""
    settings = config.build_config(document)
    parts = {name for name in ARRS if (arr := getattr(settings, name)) and not arr.dry_run}
    parts |= {
        f"{name}:queue"
        for name in ARRS
        if (arr := getattr(settings, name)) and arr.queue_cleanup.enabled and not arr.queue_cleanup.dry_run
    }
    if not settings.empty_trash.dry_run:
        parts.add("empty_trash")
    return parts


def turns_live(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """jobs a change makes live anywhere they weren't, they need confirming. Per arr, so
    making sonarr's movers live when radarr's already are still asks"""
    try:
        was = live_parts(before)
    except ValueError:
        was = set()
    newly = live_parts(after) - was
    return [job for job, parts in DRY_RUN_JOBS.items() if newly & parts]


def keep_new_arrs_dry(before: dict[str, Any], after: dict[str, Any]) -> None:
    """an arr section a save creates inherits [settings].dry_run. Connection and rule saves
    aren't confirmed, so when General is live a new arr starts dry instead, and going
    live is the arr page's change, which is"""
    if after.get("settings", {}).get("dry_run", True) is not False:
        return
    for arr in ARRS:
        if arr in after and arr not in before and "dry_run" not in after[arr]:
            after[arr]["dry_run"] = True


class LiveUnconfirmed(Exception):
    """the change makes these jobs live and wasn't confirmed"""

    def __init__(self, jobs: list[str]):
        super().__init__(f"this makes {' and '.join(jobs)} live")
        self.jobs = jobs


def save_checked(
    request: Request,
    document: dict[str, Any],
    note: str,
    version: int,
    *,
    confirmed: bool,
    secrets: dict[str, str | None] | None = None,
    check_live: bool = True,
) -> int:
    """the one way the pages and the api save the config: a change that makes a job live
    needs confirming (LiveUnconfirmed), and one from a form that can't change dry run
    (check_live=False: connections, rules, whose arr sections start dry) is never saved
    live. Raises ConfigConflict when `version` isn't current, ValueError when invalid"""
    before = store(request).document()[1]
    if not check_live:
        keep_new_arrs_dry(before, document)
        if live := turns_live(before, document):
            # a bug in the form, never save it unconfirmed
            raise ValueError(f"This change would make {' and '.join(live)} live, it wasn't saved")
    elif not confirmed and (live := turns_live(before, document)):
        raise LiveUnconfirmed(live)
    return store(request).save(document, request.state.user, note, version, secrets)


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
    """save a form's change (save_checked), confirming first when it makes a job live:
    the confirmation page posts the same fields again with confirm_live"""
    version = form_version(fields)
    if version is None:
        # every settings form carries it, without it a stale page would overwrite newer changes
        return on_error("This form didn't say which settings it was opened on, reload the page and try again")
    try:
        save_checked(
            request, document, note, version,
            confirmed=bool(first(fields, "confirm_live")), secrets=secrets, check_live=check_live,
        )
    except LiveUnconfirmed as e:
        return templates.TemplateResponse(
            request,
            "settings/confirm_live.html",
            {
                "user": request.state.user,
                "jobs": e.jobs,
                "fields": [(name, value) for name, values in fields.items() for value in values],
                "action": request.url.path,
                "back": redirect,
            },
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
