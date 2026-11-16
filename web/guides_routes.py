"""guides_routes.py - Settings → TRaSH Guides: which quality profiles follow which guide
profile, with which score set and overrides, and the custom format and quality size
options (config's [radarr.trash] / [sonarr.trash], see trash_guides)"""

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

import trash_guides
from config.models import parse_score_override
from logging_config import failure_text
from trash_guides import ArrGuide, ArrState, allowed_items, plan_arr, profile_table, suggest_links
from web import guides
from web.routes import loaded_settings
from web.settings_common import FormLists, commit, first, page, store

logger = logging.getLogger(__name__)

router = APIRouter()

ARRS = (("radarr", "Radarr"), ("sonarr", "Sonarr"))


def arr_state(request: Request, arr: str) -> tuple[Any, ArrState | None, str | None]:
    """the arr's saved settings and its custom formats, profiles and sizes now, or why
    they couldn't be read"""
    settings, error = loaded_settings(request)
    config = getattr(settings, arr, None) if settings else None
    if config is None or not config.baseurl or not config.apikey:
        return config, None, error
    try:
        return config, trash_guides.ArrSettings(config.baseurl, config.apikey, arr.upper()).state(), None
    except Exception as e:
        return config, None, f"Couldn't read {arr.title()}'s quality profiles and custom formats: {failure_text(e)}"


def comparison(arr: str, config: Any, state: ArrState | None, guide: ArrGuide | None) -> dict[str, Any] | None:
    """what the sync would plan for the saved settings now, sorted by where the page
    shows it: custom formats, quality sizes and each profile (by name, casefolded)"""
    if config is None or state is None or guide is None:
        return None
    steps, problems = plan_arr(arr.upper(), guide, state, config.trash)
    changes = [asdict(step.change) for step in steps]
    return {
        "enabled": config.trash.enabled,
        "count": len(changes),
        "problems": problems,
        "formats": [c for c in changes if c["kind"] in ("format_update", "format_add")],
        "sizes": next((c for c in changes if c["kind"] == "sizes"), None),
        "profiles": {c["title"].casefold(): c for c in changes if c["kind"] in ("profile", "profile_add")},
    }


def tables(config: Any, state: ArrState | None, guide: ArrGuide | None) -> dict[str, dict[str, Any]]:
    """each saved link's custom format table (trash_guides.profile_table), by profile
    name casefolded"""
    if config is None or state is None or guide is None:
        return {}
    found = {}
    for link in config.trash.profiles:
        if (table := profile_table(guide, state, link)) is not None:
            found[link.profile.casefold()] = table
    return found


def importable(guide: ArrGuide | None, state: ArrState | None, section: dict[str, Any]) -> list[dict[str, Any]]:
    """the guide profiles that could be imported: not linked yet, and no arr profile by
    their name (that one is linked instead)"""
    if guide is None or state is None:
        return []
    linked = {p.get("guide") for p in section.get("profiles", []) if isinstance(p, dict)}
    names = {p["name"].casefold() for p in state.profiles}
    found = []
    for trash_id, profile in guide.profiles.items():
        if trash_id in linked or profile["name"].casefold() in names:
            continue
        found.append(
            {
                "guide": trash_id,
                "name": profile["name"],
                "qualities": allowed_items(
                    {"items": [{"name": i["name"], "allowed": bool(i.get("allowed")), "items": list(i.get("items") or [])} for i in profile.get("items", [])]}
                ),
                "cutoff": profile.get("cutoff"),
                "formats": len(profile.get("formatItems") or {}),
                "score_set": profile.get("trash_score_set") or "default",
            }
        )
    return sorted(found, key=lambda p: p["name"].casefold())


def rows(section: dict[str, Any], profiles: list[dict[str, Any]], guide: ArrGuide | None) -> list[dict[str, Any]]:
    """a row per arr profile (and per linked one the arr doesn't have), its link or a
    suggestion for a profile named as in the guides, linked, suggested and missing
    ones first (shown)"""
    links = {p.get("profile", "").casefold(): p for p in section.get("profiles", []) if isinstance(p, dict)}
    suggested = suggest_links(guide, profiles) if guide else {}
    found = []
    names = [p["name"] for p in profiles]
    names += [p["profile"] for p in links.values() if p.get("profile") and p["profile"].casefold() not in {n.casefold() for n in names}]
    for name in names:
        link = links.get(name.casefold())
        found.append(
            {
                "profile": name,
                "linked": link is not None,
                "missing": name not in [p["name"] for p in profiles],
                "guide": (link or {}).get("guide", ""),
                "suggested": suggested.get(name, "") if link is None else "",
                "score_set": (link or {}).get("score_set", ""),
                "settings": bool((link or {}).get("settings")),
                "scores": "\n".join((link or {}).get("scores", [])),
                "create": bool((link or {}).get("create")),
            }
        )
    # linked, suggested and missing ones first; the page folds the rest
    for row in found:
        row["shown"] = row["linked"] or bool(row["suggested"]) or row["missing"]
    return sorted(found, key=lambda row: not row["shown"])


def guides_page(
    request: Request,
    form: dict[str, Any] | None = None,
    errors: list[str] | None = None,
) -> HTMLResponse:
    _, document = store(request).document()
    guide = guides.newest(request.app.state.engine)
    arrs = []
    for arr, label in ARRS:
        if not isinstance(document.get(arr), dict):
            continue
        section = document[arr].get("trash", {})
        config, state, problem = arr_state(request, arr)
        profiles = state.profiles if state else []
        arr_guide = guide.arr(arr) if guide else None
        values = (form or {}).get(arr) or {
            "formats": section.get("formats", False),
            "auto_formats": section.get("auto_formats", False),
            "quality_sizes": section.get("quality_sizes", ""),
            "rows": rows(section, profiles, arr_guide),
        }
        try:
            compared = comparison(arr, config, state, arr_guide)
            profile_tables = tables(config, state, arr_guide)
        except Exception as e:
            logger.warning("TRASH\t Comparing %s with the guides failed: %s", label, failure_text(e))
            compared, profile_tables = None, {}
            problem = f"Couldn't compare {label} with the guides: {failure_text(e)}"
        arrs.append(
            {
                "arr": arr,
                "label": label,
                "values": values,
                "problem": problem,
                "compared": compared,
                "tables": profile_tables,
                "importable": importable(arr_guide, state, section),
                "guide_profiles": sorted(
                    ((t, p["name"]) for t, p in arr_guide.profiles.items()), key=lambda tp: tp[1].casefold()
                ) if arr_guide else [],
                "score_sets": arr_guide.score_sets() if arr_guide else [],
                "sizes": sorted(arr_guide.sizes) if arr_guide else [],
            }
        )
    return page(
        request,
        "guides",
        {"arrs": arrs, "guide": guide, "status": guides.status(request.app.state.engine)},
        errors,
    )


def table_scores(fields: dict[str, list[str]], i: int, saved: str) -> tuple[list[str], list[str]]:
    """a profile's overrides ("name = score") and what's wrong with them. Its table
    sends cf_<i>/score_<i> pairs (an empty score follows the guide) and add_cf_<i>/
    add_score_<i>; without a table (the arr couldn't be read) the saved ones come back
    as scores_<i>"""
    names, values = fields.get(f"cf_{i}"), fields.get(f"score_{i}", [])
    pairs = list(zip(names, values)) if names is not None else []
    if add := first(fields, f"add_cf_{i}").strip():
        pairs.append((add, first(fields, f"add_score_{i}")))
        if not first(fields, f"add_score_{i}").strip():
            return [], [f"give {add} a score to add it"]
    if names is None:
        lines = [line.strip() for line in saved.splitlines() if line.strip()]
    else:
        lines = []
        for name, value in pairs:
            if value.strip():
                try:
                    lines.append(f"{name.strip()} = {int(value.strip())}")
                except ValueError:
                    return [], [f"{name}'s score must be a whole number, not '{value}'"]
    problems = []
    for line in lines:
        try:
            parse_score_override(line)
        except ValueError as e:
            problems.append(str(e))
    return lines, problems


@router.get("/settings/trash-guides", response_class=HTMLResponse)
def trash_guides_page(request: Request) -> HTMLResponse:
    return guides_page(request)


@router.post("/settings/trash-guides/check")
async def check_guides(request: Request) -> Response:
    """asks GitHub for the latest guides and downloads them when they changed"""
    try:
        await run_in_threadpool(guides.load_guides, request.app.state.engine)
    except Exception as e:
        logger.warning("TRASH\t Checking the guides failed: %s", e)
    return RedirectResponse("/settings/trash-guides", status_code=303)


@router.post("/settings/trash-guides/{arr}")
def save_trash_guides(request: Request, arr: str, fields: FormLists) -> Response:
    labels = dict(ARRS)
    _, document = store(request).document()
    if arr not in labels or not isinstance(document.get(arr), dict):
        return guides_page(request, errors=[f"{labels.get(arr, arr)} isn't connected"])
    label = labels[arr]
    errors: list[str] = []
    form_rows = []
    profiles = []
    count = len(fields.get("profile", []))
    for i in range(count):
        row = {
            "profile": fields["profile"][i].strip(),
            "guide": first(fields, f"guide_{i}"),
            "score_set": first(fields, f"score_set_{i}"),
            "settings": bool(first(fields, f"settings_{i}")),
            "scores": first(fields, f"scores_{i}"),
            "create": bool(first(fields, f"create_{i}")),
            "linked": bool(first(fields, f"guide_{i}")),
            "missing": False,
            "suggested": "",
        }
        row["missing"] = row["create"]
        row["shown"] = row["linked"] or row["create"]
        form_rows.append(row)
        if not row["guide"]:
            continue
        scores, problems = table_scores(fields, i, row["scores"])
        errors += [f"{label} {row['profile']}: {p}" for p in problems]
        row["scores"] = "\n".join(scores)
        link: dict[str, Any] = {"profile": row["profile"], "guide": row["guide"]}
        if row["score_set"]:
            link["score_set"] = row["score_set"]
        if row["settings"] or row["create"]:
            link["settings"] = True
        if scores:
            link["scores"] = scores
        if row["create"]:
            link["create"] = True
        profiles.append(link)
    values = {
        "formats": bool(first(fields, "formats")),
        "auto_formats": bool(first(fields, "auto_formats")),
        "quality_sizes": first(fields, "quality_sizes"),
        "rows": form_rows,
    }
    if values["auto_formats"] and not values["formats"]:
        errors.append(f"{label}: applying format fixes without review needs Keep custom formats in line on")
    if errors:
        return guides_page(request, {arr: values}, errors)
    section: dict[str, Any] = {}
    if values["formats"]:
        section["formats"] = True
    if values["auto_formats"]:
        section["auto_formats"] = True
    if values["quality_sizes"]:
        section["quality_sizes"] = values["quality_sizes"]
    if profiles:
        section["profiles"] = profiles
    if section:
        document[arr]["trash"] = section
    else:
        document[arr].pop("trash", None)
    return commit(
        request, fields, document, f"{label} TRaSH Guides settings",
        lambda e: guides_page(request, {arr: values}, [e]), "/settings/trash-guides",
    )


@router.post("/settings/trash-guides/{arr}/import")
def import_profiles(request: Request, arr: str, fields: FormLists) -> Response:
    """links the chosen guide profiles as new ones, which the next sync plans making"""
    labels = dict(ARRS)
    _, document = store(request).document()
    if arr not in labels or not isinstance(document.get(arr), dict):
        return guides_page(request, errors=[f"{labels.get(arr, arr)} isn't connected"])
    label = labels[arr]
    guide = guides.newest(request.app.state.engine)
    chosen = [g for g in fields.get("guide", []) if g]
    if guide is None:
        return guides_page(request, errors=["Download the guides first"])
    if not chosen:
        return guides_page(request, errors=[f"{label}: choose a guide profile to import"])
    arr_guide = guide.arr(arr)
    _, state, problem = arr_state(request, arr)
    if state is None:
        return guides_page(request, errors=[problem or f"{label} couldn't be read"])
    section = document[arr].setdefault("trash", {})
    links = section.setdefault("profiles", [])
    taken = {p["name"].casefold() for p in state.profiles} | {p.get("profile", "").casefold() for p in links}
    errors = []
    for trash_id in chosen:
        profile = arr_guide.profiles.get(trash_id)
        if profile is None:
            errors.append(f"{label}: the guides have no quality profile {trash_id}")
        elif profile["name"].casefold() in taken:
            errors.append(f"{label} already has a profile named {profile['name']}: link it instead")
        else:
            links.append({"profile": profile["name"], "guide": trash_id, "settings": True, "create": True})
            taken.add(profile["name"].casefold())
    if errors:
        return guides_page(request, errors=errors)
    names = ", ".join(arr_guide.profiles[t]["name"] for t in chosen)
    return commit(
        request, fields, document, f"{label} TRaSH Guides: import {names}",
        lambda e: guides_page(request, errors=[e]), "/settings/trash-guides",
    )
