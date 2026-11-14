"""guides_routes.py - Settings → TRaSH Guides: which quality profiles follow which guide
profile, with which score set and overrides, and the custom format and quality size
options (config's [radarr.trash] / [sonarr.trash], see trash_guides)"""

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from arr_client import ArrV3
from config.models import parse_score_override
from logging_config import failure_text
from trash_guides import ArrGuide, suggest_links
from web import guides
from web.routes import loaded_settings
from web.settings_common import FormLists, commit, first, page, store

logger = logging.getLogger(__name__)

router = APIRouter()

ARRS = (("radarr", "Radarr"), ("sonarr", "Sonarr"))


def arr_profiles(request: Request, arr: str) -> tuple[list[dict[str, Any]], str | None]:
    """the arr's quality profiles, or none and why"""
    settings, error = loaded_settings(request)
    config = getattr(settings, arr, None) if settings else None
    if config is None or not config.baseurl or not config.apikey:
        return [], error
    try:
        return ArrV3(config.baseurl, config.apikey)._request("GET", "qualityprofile"), None
    except Exception as e:
        return [], f"Couldn't read the quality profiles: {failure_text(e)}"


def rows(section: dict[str, Any], profiles: list[dict[str, Any]], guide: ArrGuide | None) -> list[dict[str, Any]]:
    """a row per arr profile (and per linked one the arr doesn't have), its link or a
    suggestion for a profile named as in the guides"""
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
            }
        )
    return found


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
        profiles, problem = arr_profiles(request, arr)
        arr_guide = guide.arr(arr) if guide else None
        values = (form or {}).get(arr) or {
            "formats": section.get("formats", False),
            "auto_formats": section.get("auto_formats", False),
            "quality_sizes": section.get("quality_sizes", ""),
            "rows": rows(section, profiles, arr_guide),
        }
        arrs.append(
            {
                "arr": arr,
                "label": label,
                "values": values,
                "problem": problem,
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
            "linked": bool(first(fields, f"guide_{i}")),
            "missing": False,
            "suggested": "",
        }
        form_rows.append(row)
        if not row["guide"]:
            continue
        scores = [line.strip() for line in row["scores"].splitlines() if line.strip()]
        for line in scores:
            try:
                parse_score_override(line)
            except ValueError as e:
                errors.append(f"{label} {row['profile']}: {e}")
        link: dict[str, Any] = {"profile": row["profile"], "guide": row["guide"]}
        if row["score_set"]:
            link["score_set"] = row["score_set"]
        if row["settings"]:
            link["settings"] = True
        if scores:
            link["scores"] = scores
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
