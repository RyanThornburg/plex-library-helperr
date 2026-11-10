"""rule_routes.py - Settings → Radarr/Sonarr and their mover, profiler and remover rule
forms. Registered after the other settings routes: /settings/{arr} would catch
/settings/trash"""

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

import config
from config.document import Rule, rule_class, rule_to_dict
from config.models import REMOVER_TYPES
from web.lookups import (
    ArrLibrary,
    LookupFailed,
    Preview,
    preview_mover,
    preview_profiler,
)
from web.routes import error_page, loaded_settings, templates
from web.settings_common import (
    ARR_FIELD,
    RULE_NAMES,
    SERVICES,
    check_arr,
    check_kind,
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


logger = logging.getLogger(__name__)

router = APIRouter()


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
    global_dry = document.get("settings", {}).get("dry_run", True)
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
def save_arr(request: Request, arr: str, fields: FormLists) -> Response:
    check_arr(arr)
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
    settings, error = loaded_settings(request)
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
    """whether rule `number` isn't the rule the form was opened on: another tab deleted or
    moved one. A form that doesn't say counts as changed, rules are saved by number"""
    rules = document.get(arr, {}).get(kind, [])
    loaded = first(fields, "loaded")
    return not loaded or not 1 <= number <= len(rules) or loaded != fingerprint(rules[number - 1])


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
    settings, _ = loaded_settings(request)
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


def _mover_fields(fields: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "path": first(fields, "path"),
        "plex_library": first(fields, "plex_library") or None,
        "genre_match": first(fields, "genre_match") or "any",
        "tag_match": first(fields, "tag_match") or "any",
    }


def _profiler_fields(fields: dict[str, list[str]]) -> dict[str, Any]:
    monitored = first(fields, "monitored")
    return {"profile": first(fields, "profile"), "monitored": None if monitored == "" else monitored == "true"}


def _movie_remover_fields(fields: dict[str, list[str]]) -> dict[str, Any]:
    # always, ever or within, kept so the form shows it again after an error
    watched = first(fields, "watched") or "always"
    return {
        "path": first(fields, "path"),
        "min_age": first(fields, "min_age"),
        "watched": watched,
        "skip_if_ever_watched": watched == "ever",
        "skip_if_watched_within": first(fields, "skip_if_watched_within") or None if watched == "within" else None,
        "plex_library": first(fields, "plex_library") or None,
    }


def _played_within(data: dict[str, Any]) -> None:
    if data.pop("watched") == "within" and not data["skip_if_watched_within"]:
        raise ValueError("Played within is required to keep movies played recently")


def _series_remover_fields(fields: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "type": first(fields, "type"),
        "keep": first(fields, "keep"),
        "min_age": first(fields, "min_age"),
        "skip_if_watched_within": first(fields, "skip_if_watched_within") or None,
        "include_specials": "include_specials" in fields,
        "plex_library": first(fields, "plex_library") or None,
    }


@dataclass(frozen=True)
class RuleForm:
    """one kind of rule form, which rule_values() and build_rule() follow"""

    # one value per line; ARR stands for the arr's own field (studios or networks)
    lists: tuple[str, ...]
    # its other fields, as the rule's document form (not validated yet)
    read: Callable[[dict[str, list[str]]], dict[str, Any]]
    # a field it can't be saved without, and what to say
    required: tuple[str, str] | None = None
    # whole numbers of at least 1: field, label
    ints: tuple[tuple[str, str], ...] = ()
    # durations (7d): field, label, whether it may be empty
    durations: tuple[tuple[str, str, bool], ...] = ()
    # anything else to check, raising ValueError
    check: Callable[[dict[str, Any]], None] | None = None


ARR = "{arr field}"
REMOVER_DURATIONS = (("min_age", "Older than", False), ("skip_if_watched_within", "Watched within", True))
RULE_FORMS: dict[tuple[str | None, str], RuleForm] = {
    (None, "movers"): RuleForm(
        ("exclude_paths", "certifications", "exclude_certifications", "genres", "exclude_genres", ARR, "tags", "exclude_tags"),
        _mover_fields,
        required=("path", "Move to is required"),
    ),
    (None, "profilers"): RuleForm(("titles", ARR), _profiler_fields, required=("profile", "Quality profile is required")),
    ("radarr", "removers"): RuleForm(
        ("exclude_tags",), _movie_remover_fields,
        required=("path", "Root folder is required"), durations=REMOVER_DURATIONS, check=_played_within,
    ),
    ("sonarr", "removers"): RuleForm(
        ("tags", "exclude_tags"), _series_remover_fields, ints=(("keep", "Keep"),), durations=REMOVER_DURATIONS,
    ),
}


def rule_form_of(arr: str, kind: str) -> RuleForm:
    return RULE_FORMS.get((arr, kind)) or RULE_FORMS[None, kind]


def rule_values(kind: str, arr: str, fields: dict[str, list[str]]) -> dict[str, Any]:
    """a rule form's fields, as the rule's document form (not validated yet)"""
    form = rule_form_of(arr, kind)
    values: dict[str, Any] = {
        (ARR_FIELD[arr] if name == ARR else name): lines(fields, ARR_FIELD[arr] if name == ARR else name)
        for name in form.lists
    }
    return values | form.read(fields)


def build_rule(arr: str, kind: str, values: dict[str, Any]) -> Rule:
    """the rule from a form's values, ValueError saying what's wrong"""
    form = rule_form_of(arr, kind)
    data = dict(values)
    if form.required and not data[form.required[0]]:
        raise ValueError(form.required[1])
    if form.check:
        form.check(data)
    errors: list[str] = []
    for name, label in form.ints:
        data[name] = checked_int(data[name], label, errors, 1)
    for name, label, optional in form.durations:
        if data[name] or not optional:
            checked_duration(data[name], label, errors)
    if errors:
        raise ValueError("; ".join(errors))
    for name, label, _ in form.durations:
        if data[name]:
            data[name] = config.parse_duration(data[name], label)
    return rule_class(arr, kind)(**data)


@router.get("/settings/{arr}/{kind}/new", response_class=HTMLResponse)
def new_rule_page(request: Request, arr: str, kind: str) -> HTMLResponse:
    check_kind(arr, kind)
    return rule_form(request, arr, kind, None, {})


@router.get("/settings/{arr}/{kind}/{number}", response_class=HTMLResponse)
def edit_rule_page(request: Request, arr: str, kind: str, number: int) -> HTMLResponse:
    check_kind(arr, kind)
    _, document = store(request).document()
    rule = rule_number(document, arr, kind, number)
    return rule_form(request, arr, kind, number, rule, loaded=fingerprint(rule))


@router.post("/settings/{arr}/{kind}/new")
def create_rule(request: Request, arr: str, kind: str, fields: FormLists) -> Response:
    check_kind(arr, kind)
    return save_rule(request, arr, kind, None, fields)


@router.post("/settings/{arr}/{kind}/{number}")
def update_rule(request: Request, arr: str, kind: str, number: int, fields: FormLists) -> Response:
    check_kind(arr, kind)
    return save_rule(request, arr, kind, number, fields)


def save_rule(request: Request, arr: str, kind: str, number: int | None, fields: FormLists) -> Response:
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
    settings, error = loaded_settings(request)
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
def delete_rule(request: Request, arr: str, kind: str, number: int, fields: FormLists) -> Response:
    check_kind(arr, kind)
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
def move_rule(request: Request, arr: str, kind: str, number: int, direction: str, fields: FormLists) -> Response:
    """movers run in order and the first match wins, so their order matters"""
    check_kind(arr, kind)
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
def save_remover_settings(request: Request, arr: str, fields: FormLists) -> Response:
    check_arr(arr)
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
