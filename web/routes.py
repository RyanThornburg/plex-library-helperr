"""routes.py - pages and json api for running jobs, reviewing runs and scheduling jobs"""

import logging
import pathlib
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import config
from config.models import ConfigSettings, MovieRemoverRule, RemoverRule
from results import JOBS
from web.db import JobRun, RemoverRuleSetting, Schedule
from web.jobs import (
    ACTIVE,
    APPROVABLE,
    AlreadyActive,
    ApplyRefused,
    JobRunner,
    approvable,
    removal_key,
    utc_now,
)
from web.secret_box import SecretError
from web.scheduler import ZONES, next_run_at, schedule_errors, trash_followup_due

logger = logging.getLogger(__name__)

router = APIRouter()

templates = Jinja2Templates(directory=pathlib.Path(__file__).parent / "templates")

JOB_INFO: dict[str, tuple[str, str]] = {
    "movers": (
        "Movers & profilers",
        "Move media to the root folders in your mover rules and set quality profiles.",
    ),
    "removers": (
        "Removers",
        "Plan deleting old seasons/episodes of tagged series and old movies in root folders.",
    ),
    "empty_trash": (
        "Empty Plex trash",
        "Clear trashed items, skipping libraries that are scanning or over max_items.",
    ),
}


def localtime(value: datetime | None) -> str:
    """stored times are naive utc. Rendered as utc in a <time>, which static/app.js
    rewrites in the browser's time zone"""
    if value is None:
        return "-"
    return Markup('<time datetime="{}Z">{} UTC</time>').format(
        value.isoformat(timespec="seconds"),
        value.strftime("%Y-%m-%d %H:%M:%S"),
    )


def localdate(value: datetime | None) -> str:
    """localtime for dense tables: static/app.js shows the browser's date, the full
    time in its title"""
    if value is None:
        return "-"
    return Markup('<time datetime="{}Z" data-date>{}</time>').format(
        value.isoformat(timespec="seconds"),
        value.strftime("%Y-%m-%d"),
    )


def duration(run: JobRun) -> str:
    if not run.started_at or not run.finished_at:
        return "-"
    seconds = int((run.finished_at - run.started_at).total_seconds())
    return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"


def run_source(run: JobRun) -> str:
    if run.requested_by:
        return run.requested_by
    if run.schedule:
        return f"schedule: {run.schedule.name}"
    if run.trigger == "followup":
        return "follow-up after moves/removals/upgrades"
    return run.trigger


templates.env.filters["localtime"] = localtime
templates.env.filters["localdate"] = localdate
templates.env.globals["duration"] = duration
templates.env.globals["run_source"] = run_source
templates.env.globals["JOB_INFO"] = JOB_INFO
templates.env.globals["APPROVABLE"] = APPROVABLE
templates.env.globals["removal_key"] = removal_key

# a schedule form is a few short fields
MAX_FORM_BYTES = 10_000


def runner(request: Request) -> JobRunner:
    return request.app.state.runner


def get_run(request: Request, run_id: int) -> JobRun:
    with Session(request.app.state.engine) as session:
        run = session.get(JobRun, run_id)
    if run is None:
        raise HTTPException(status_code=404)
    return run


CONNECTION_LABELS = {"seerr": "Seerr", "tautulli": "Tautulli", "radarr": "Radarr", "sonarr": "Sonarr", "plex": "Plex"}


def missing_connections(request: Request, *services: str) -> list[str]:
    """the connections a page needs that aren't set up. [] when the settings can't
    load: the page's sync status says why"""
    try:
        settings = request.app.state.load_config()
    except (SecretError, ValueError):
        return []
    return [CONNECTION_LABELS[s] for s in services if getattr(settings, s) is None]


def recent_runs(request: Request, limit: int, before: int | None = None) -> list[JobRun]:
    """newest first, those before a run id when paging back"""
    query = select(JobRun).order_by(JobRun.id.desc()).limit(limit)
    if before is not None:
        query = query.where(JobRun.id < before)
    with Session(request.app.state.engine) as session:
        return list(session.scalars(query))


def run_to_dict(run: JobRun, include_log: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": run.id,
        "job": run.job,
        "status": run.status,
        "dry_run": run.dry_run,
        "trigger": run.trigger,
        "requested_by": run.requested_by,
        "created_at": run.created_at.isoformat() + "Z",
        "started_at": run.started_at.isoformat() + "Z" if run.started_at else None,
        "finished_at": run.finished_at.isoformat() + "Z" if run.finished_at else None,
        "result": run.result,
        "schedule_id": run.schedule_id,
        "plan_run_id": run.plan_run_id,
        "approved": run.approved,
    }
    if include_log:
        data["log"] = run.log
    return data


def check_job(job: str) -> None:
    if job not in JOBS:
        raise HTTPException(status_code=404)


def all_removers(settings: ConfigSettings) -> list[RemoverRule | MovieRemoverRule]:
    """sonarr's removers, then radarr's"""
    return [r for arr in (settings.sonarr, settings.radarr) if arr for r in arr.removers]


def auto_approved(
    request: Request, settings: ConfigSettings
) -> list[RemoverRule | MovieRemoverRule]:
    """the configured removers that delete without review"""
    keys = runner(request).auto_approved_rules()
    return [r for r in all_removers(settings) if r.key in keys] if keys else []


def config_modes(
    request: Request,
) -> tuple[dict[str, bool | None], str | None, list[RemoverRule | MovieRemoverRule]]:
    """each job's dry run state as configured (None when partly live), any error loading
    the config and the auto-approved removers"""
    try:
        settings = request.app.state.load_config()
    except Exception as e:  # shown on the page, jobs would fail the same way
        return {}, f"{type(e).__name__}: {e}", []
    modes = {job: config.config_dry_run(job, settings) for job in JOBS}
    auto = auto_approved(request, settings)
    if auto and modes["removers"]:
        # auto-approved removers delete even with remover dry_run on
        modes["removers"] = None
    return modes, None, auto


def span(value: timedelta | None) -> str:
    """a duration the way the settings write it: 7d, 12h, 30m"""
    return "-" if value is None else config.format_duration(value)


templates.env.filters["span"] = span


def all_schedules(request: Request) -> list[Schedule]:
    with Session(request.app.state.engine) as session:
        return list(session.scalars(select(Schedule).order_by(Schedule.name, Schedule.id)))


def get_schedule(request: Request, schedule_id: int) -> Schedule:
    with Session(request.app.state.engine) as session:
        schedule = session.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404)
    return schedule


def save_schedule(request: Request, schedule_id: int | None, values: dict[str, Any]) -> int:
    """create (schedule_id None) or update a validated schedule, returns its id"""
    now = utc_now()
    with Session(request.app.state.engine) as session:
        if schedule_id is None:
            schedule = Schedule(created_at=now)
            session.add(schedule)
        else:
            schedule = session.get(Schedule, schedule_id)
            if schedule is None:
                raise HTTPException(status_code=404)
        for key, value in values.items():
            setattr(schedule, key, value)
        schedule.updated_at = now
        session.commit()
        return schedule.id


def delete_schedule(request: Request, schedule_id: int) -> None:
    """its runs are kept, their schedule_id becomes null"""
    with Session(request.app.state.engine) as session:
        schedule = session.get(Schedule, schedule_id)
        if schedule is None:
            raise HTTPException(status_code=404)
        session.delete(schedule)
        session.commit()


def last_runs(request: Request) -> dict[int, JobRun]:
    """each schedule's latest run"""
    with Session(request.app.state.engine) as session:
        latest = (
            select(JobRun.schedule_id, func.max(JobRun.id).label("id"))
            .where(JobRun.schedule_id.is_not(None))
            .group_by(JobRun.schedule_id)
            .subquery()
        )
        runs = session.scalars(select(JobRun).join(latest, JobRun.id == latest.c.id))
        return {run.schedule_id: run for run in runs.unique() if run.schedule_id}


def schedule_to_dict(schedule: Schedule, now: datetime) -> dict[str, Any]:
    next_run = next_run_at(schedule, now)
    return {
        "id": schedule.id,
        "name": schedule.name,
        "job": schedule.job,
        "cron": schedule.cron,
        "timezone": schedule.timezone,
        "dry_run": schedule.dry_run,
        "enabled": schedule.enabled,
        "created_at": schedule.created_at.isoformat() + "Z",
        "updated_at": schedule.updated_at.isoformat() + "Z",
        "last_fired_at": (
            schedule.last_fired_at.isoformat() + "Z" if schedule.last_fired_at else None
        ),
        "next_run_at": next_run.isoformat() + "Z" if next_run else None,
    }


async def form_lists(request: Request) -> dict[str, list[str]]:
    """a urlencoded form body with every value of each field, without python-multipart.
    Unchecked boxes are absent"""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise HTTPException(status_code=413)
    return parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)


async def form_fields(request: Request) -> dict[str, str]:
    """a urlencoded form body, the last value of each field"""
    return {key: values[-1] for key, values in (await form_lists(request)).items()}


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


# runs, older ones a page at a time
RUNS_PAGE = 25


@router.get("/")
def home() -> RedirectResponse:
    """Status answers "is anything wrong" first, so it's the landing page"""
    return RedirectResponse("/status", status_code=303)


@router.get("/runs", response_class=HTMLResponse)
def index(request: Request, before: int | None = Query(default=None, ge=1, le=2**62)) -> HTMLResponse:
    dry_runs, config_error, auto_rules = config_modes(request)
    runs = recent_runs(request, RUNS_PAGE + 1, before)
    older, runs = len(runs) > RUNS_PAGE, runs[:RUNS_PAGE]
    # the jobs running now are among the newest runs
    latest = runs if before is None else recent_runs(request, RUNS_PAGE)
    # each job's soonest scheduled run
    now = utc_now()
    next_runs: dict[str, tuple[datetime, Schedule]] = {}
    for schedule in all_schedules(request):
        next_run = next_run_at(schedule, now)
        if next_run and (schedule.job not in next_runs or next_run < next_runs[schedule.job][0]):
            next_runs[schedule.job] = (next_run, schedule)
    with Session(request.app.state.engine) as session:
        followup = trash_followup_due(session, now, runner(request).defaults())
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": request.state.user,
            "runs": runs,
            "before": before,
            "older": older,
            "no_arrs": len(missing_connections(request, "radarr", "sonarr")) == 2,
            "jobs": JOBS,
            "dry_runs": dry_runs,
            "config_error": config_error,
            "auto_rules": auto_rules,
            "active_jobs": {r.job for r in latest if r.status in ACTIVE},
            "next_runs": next_runs,
            "followup": followup,
        },
    )


@router.get("/jobs/{job}/confirm", response_class=HTMLResponse)
def confirm_page(request: Request, job: str) -> HTMLResponse:
    """runs with the config's dry run settings go through this page, it says whether
    they're live"""
    check_job(job)
    dry_runs, config_error, auto_rules = config_modes(request)
    mode = dry_runs.get(job)
    return templates.TemplateResponse(
        request,
        "confirm.html",
        {
            "user": request.state.user,
            "job": job,
            "mode": mode,
            "config_error": config_error,
            "auto_rules": auto_rules if job == "removers" else [],
        },
    )


ERROR_TITLES = {400: "That wasn't saved", 404: "Not found", 409: "Not now", 413: "Too large", 422: "That can't be shown"}


def error_page(request: Request, status_code: int, detail: str, back: tuple[str, str] | None = None) -> HTMLResponse:
    """an error as a page saying why, with a way back (label, url)"""
    return templates.TemplateResponse(
        request,
        "error.html",
        {
            "user": getattr(request.state, "user", None),
            "title": ERROR_TITLES.get(status_code, "Something went wrong"),
            "detail": detail,
            "back": back,
        },
        status_code=status_code,
    )


@router.post("/jobs/{job}")
def run_job_form(request: Request, job: str, dry_run: bool = True) -> RedirectResponse:
    """the page's buttons post here with ?dry_run=..., no request body to parse"""
    check_job(job)
    try:
        run_id = runner(request).submit(job, dry_run, "manual", request.state.user)
    except AlreadyActive as e:
        # that run's page says this one wasn't queued
        return RedirectResponse(f"/runs/{e.run_id}?busy=1", status_code=303)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int) -> HTMLResponse:
    run = get_run(request, run_id)
    result = run.result or {}
    removals = result.get("removals", [])
    # removals are shown in their own table with their files, so not again as changes
    planned = {(r["title"], r["status"]) for r in removals}
    changes = [
        c
        for c in result.get("changes", [])
        if not (c["action"] == "remove" and (c["target"], c["status"]) in planned)
    ]
    with Session(request.app.state.engine) as session:
        problem = runner(request).apply_problem(session, run) if approvable(run) else None
        applied_by = list(session.scalars(select(JobRun.id).where(JobRun.plan_run_id == run.id)))
    return templates.TemplateResponse(
        request,
        "run.html",
        {
            "user": request.state.user,
            "run": run,
            "active": run.status in ACTIVE,
            "removals": removals,
            "changes": changes,
            "selectable": bool(approvable(run)) and problem is None,
            "apply_problem": problem,
            "applied_by": applied_by,
            "busy": request.query_params.get("busy") == "1",
        },
    )


def apply_confirm(
    request: Request, run_id: int, items: list[str], error: str | None = None
) -> HTMLResponse:
    run = get_run(request, run_id)
    selected = [r for r in approvable(run) if removal_key(r) in set(items)]
    with Session(request.app.state.engine) as session:
        problem = error or runner(request).apply_problem(session, run)
    if problem is None and (not selected or len(selected) != len(set(items))):
        problem = "Select at least one series or movie from the plan."
    # each arr's removals and files, checked against its own max_deletes
    arrs: list[dict[str, Any]] = []
    try:
        settings = request.app.state.load_config()
    except Exception:  # the run would fail the same way, its log will say why
        settings = None
    for service in ("SONARR", "RADARR"):
        removals = [r for r in selected if r["service"] == service]
        if not removals:
            continue
        arr = getattr(settings, service.lower()) if settings else None
        max_deletes = arr.remover_settings.max_deletes if arr else None
        files = sum(len(r["files"]) for r in removals)
        arrs.append(
            {
                "service": service,
                "removals": removals,
                "files": files,
                "max_deletes": max_deletes,
                "over_max": max_deletes is not None and files > max_deletes,
            }
        )
    return templates.TemplateResponse(
        request,
        "apply_confirm.html",
        {
            "user": request.state.user,
            "run": run,
            "selected": selected,
            "arrs": arrs,
            "total": sum(a["files"] for a in arrs),
            "problem": problem,
            "over_max": any(a["over_max"] for a in arrs),
            "query": urlencode([("item", removal_key(r)) for r in selected]),
        },
        status_code=400 if error else 200,
    )


@router.get("/runs/{run_id}/apply", response_class=HTMLResponse)
def apply_confirm_page(
    request: Request, run_id: int, item: list[str] = Query(default=[])
) -> HTMLResponse:
    """the plan's form gets here with ?item=sonarr:1&item=radarr:2..., says exactly what
    will be deleted"""
    return apply_confirm(request, run_id, item)


@router.post("/runs/{run_id}/apply", response_model=None)
def apply_plan_form(
    request: Request, run_id: int, item: list[str] = Query(default=[])
) -> Response:
    try:
        new_id = runner(request).submit_apply(run_id, item, "manual", request.state.user)
    except LookupError:
        raise HTTPException(status_code=404) from None
    except ApplyRefused as e:
        return apply_confirm(request, run_id, item, str(e))
    except AlreadyActive as e:
        response = apply_confirm(
            request, run_id, item,
            f"Removers run #{e.run_id} is active, so the plan wasn't applied. Apply it once that run finishes.",
        )
        response.status_code = 409
        return response
    return RedirectResponse(f"/runs/{new_id}", status_code=303)


def schedule_form(
    request: Request,
    schedule_id: int | None,
    values: dict[str, Any],
    errors: list[str] | None = None,
) -> HTMLResponse:
    dry_runs, config_error, auto_rules = config_modes(request)
    next_run = None if errors else next_run_at(Schedule(**values), utc_now())
    return templates.TemplateResponse(
        request,
        "schedule_form.html",
        {
            "user": request.state.user,
            "schedule_id": schedule_id,
            "values": values,
            "errors": errors or [],
            "jobs": JOBS,
            "dry_runs": dry_runs,
            "config_error": config_error,
            "auto_rules": auto_rules,
            "next_run": next_run,
            "zones": ZONES,
        },
        status_code=400 if errors else 200,
    )


def form_values(fields: dict[str, str]) -> dict[str, Any]:
    return {
        "name": fields.get("name", "").strip(),
        "job": fields.get("job", ""),
        "cron": fields.get("cron", "").strip(),
        "timezone": fields.get("timezone", "").strip(),
        "dry_run": "dry_run" in fields,
        "enabled": "enabled" in fields,
    }


def schedule_values(schedule: Schedule) -> dict[str, Any]:
    return {
        "name": schedule.name,
        "job": schedule.job,
        "cron": schedule.cron,
        "timezone": schedule.timezone,
        "dry_run": schedule.dry_run,
        "enabled": schedule.enabled,
    }


@router.get("/schedules", response_class=HTMLResponse)
def schedules_page(request: Request) -> HTMLResponse:
    now = utc_now()
    schedules = all_schedules(request)
    return templates.TemplateResponse(
        request,
        "schedules.html",
        {
            "user": request.state.user,
            "schedules": schedules,
            "next_runs": {s.id: next_run_at(s, now) for s in schedules},
            "last_runs": last_runs(request),
        },
    )


@router.get("/schedules/new", response_class=HTMLResponse)
def new_schedule_page(request: Request) -> HTMLResponse:
    # dry by default, like the api: a live schedule has to be asked for. The time zone
    # is replaced with the browser's by static/app.js
    values = {
        "name": "",
        "job": JOBS[0],
        "cron": "",
        "timezone": "UTC",
        "dry_run": True,
        "enabled": True,
    }
    return schedule_form(request, None, values)


@router.post("/schedules")
async def create_schedule_form(request: Request) -> Response:
    values = form_values(await form_fields(request))
    if errors := schedule_errors(
        values["name"], values["job"], values["cron"], values["timezone"]
    ):
        return schedule_form(request, None, values, errors)
    save_schedule(request, None, values)
    return RedirectResponse("/schedules", status_code=303)


@router.get("/schedules/{schedule_id}", response_class=HTMLResponse)
def edit_schedule_page(request: Request, schedule_id: int) -> HTMLResponse:
    schedule = get_schedule(request, schedule_id)
    return schedule_form(request, schedule_id, schedule_values(schedule))


@router.post("/schedules/{schedule_id}")
async def update_schedule_form(request: Request, schedule_id: int) -> Response:
    get_schedule(request, schedule_id)
    values = form_values(await form_fields(request))
    if errors := schedule_errors(
        values["name"], values["job"], values["cron"], values["timezone"]
    ):
        return schedule_form(request, schedule_id, values, errors)
    save_schedule(request, schedule_id, values)
    return RedirectResponse("/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/toggle")
def toggle_schedule(request: Request, schedule_id: int) -> RedirectResponse:
    schedule = get_schedule(request, schedule_id)
    save_schedule(request, schedule_id, {"enabled": not schedule.enabled})
    return RedirectResponse("/schedules", status_code=303)


@router.post("/schedules/{schedule_id}/delete")
def delete_schedule_form(request: Request, schedule_id: int) -> RedirectResponse:
    delete_schedule(request, schedule_id)
    return RedirectResponse("/schedules", status_code=303)


def remover_rules(request: Request) -> tuple[list[RemoverRule | MovieRemoverRule], str | None]:
    settings, error = remover_config(request)
    return (all_removers(settings) if settings else []), error


def remover_config(request: Request) -> tuple[ConfigSettings | None, str | None]:
    try:
        return request.app.state.load_config(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def rule_settings(request: Request) -> dict[str, RemoverRuleSetting]:
    with Session(request.app.state.engine) as session:
        return {s.rule_key: s for s in session.scalars(select(RemoverRuleSetting))}


def set_auto_approve(request: Request, rule_key: str, enabled: bool) -> None:
    """turning on needs the rule in the config, off always works"""
    if enabled and rule_key not in {r.key for r in remover_rules(request)[0]}:
        raise HTTPException(status_code=404)
    with Session(request.app.state.engine) as session:
        setting = session.get(RemoverRuleSetting, rule_key) or RemoverRuleSetting(
            rule_key=rule_key
        )
        setting.auto_approve = enabled
        setting.updated_at = utc_now()
        setting.updated_by = request.state.user
        session.add(setting)
        session.commit()
    logger.info(
        "WEB\t Auto-approve %s for remover %s by %s",
        "on" if enabled else "off",
        rule_key,
        request.state.user,
    )


@router.get("/removers", response_class=HTMLResponse)
def removers_page(request: Request) -> HTMLResponse:
    settings, config_error = remover_config(request)
    sonarr = settings.sonarr if settings else None
    radarr = settings.radarr if settings else None
    return templates.TemplateResponse(
        request,
        "removers.html",
        {
            "user": request.state.user,
            "rules": sonarr.removers if sonarr else [],
            "movie_rules": radarr.removers if radarr else [],
            "max_deletes": sonarr.remover_settings.max_deletes if sonarr else None,
            "movie_max_deletes": radarr.remover_settings.max_deletes if radarr else None,
            "config_error": config_error,
            "settings": rule_settings(request),
            "allow_apply": runner(request).allow_apply,
            "version": request.app.state.store.document()[0],
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.get("/removers/auto-approve", response_class=HTMLResponse)
def auto_approve_confirm_page(request: Request, rule: str) -> HTMLResponse:
    """turning auto-approve on always goes through this page"""
    matching = [r for r in remover_rules(request)[0] if r.key == rule]
    if not matching:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        request,
        "auto_approve_confirm.html",
        {
            "user": request.state.user,
            "rule": matching[0],
            "allow_apply": runner(request).allow_apply,
            "query": urlencode({"rule": rule, "enabled": "true"}),
        },
    )


@router.post("/removers/auto-approve")
def auto_approve_form(request: Request, rule: str, enabled: bool) -> RedirectResponse:
    set_auto_approve(request, rule, enabled)
    return RedirectResponse("/removers", status_code=303)


# ---------------------------------------------------------------------------
# json api
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    # dry by default: a live run has to be asked for
    dry_run: bool = True


@router.post("/api/jobs/{job}", status_code=202)
def api_run_job(request: Request, job: str, body: RunRequest) -> JSONResponse:
    check_job(job)
    try:
        run_id = runner(request).submit(job, body.dry_run, "api", request.state.user)
    except AlreadyActive as e:
        return JSONResponse({"error": "already active", "id": e.run_id}, status_code=409)
    return JSONResponse({"id": run_id}, status_code=202)


class ApplyRequest(BaseModel):
    # removal keys: sonarr:<series id> or radarr:<movie id>
    items: list[str] = []


@router.post("/api/runs/{run_id}/apply", status_code=202)
def api_apply_plan(request: Request, run_id: int, body: ApplyRequest) -> JSONResponse:
    """removes the approved series/movies of a removers run's plan"""
    try:
        new_id = runner(request).submit_apply(run_id, body.items, "api", request.state.user)
    except LookupError:
        raise HTTPException(status_code=404) from None
    except ApplyRefused as e:
        return JSONResponse({"error": str(e)}, status_code=422)
    except AlreadyActive as e:
        return JSONResponse({"error": "already active", "id": e.run_id}, status_code=409)
    return JSONResponse({"id": new_id}, status_code=202)


class AutoApproveRequest(BaseModel):
    # RemoverRule.key, its tags casefolded, sorted and comma joined, or
    # MovieRemoverRule.key, radarr:<root folder>
    rule: str
    enabled: bool


@router.get("/api/removers")
def api_removers(request: Request) -> list[dict[str, Any]]:
    """the config's removers with their auto-approve setting"""
    rules, config_error = remover_rules(request)
    if config_error:
        raise HTTPException(status_code=503, detail=config_error)
    settings = rule_settings(request)
    return [
        {
            "rule": r.key,
            "auto_approve": r.key in settings and settings[r.key].auto_approve,
            **(
                {"service": "SONARR", "type": r.type, "keep": r.keep, "tags": r.tags}
                if isinstance(r, RemoverRule)
                else {"service": "RADARR", "path": r.root}
            ),
        }
        for r in rules
    ]


@router.post("/api/removers/auto-approve")
def api_auto_approve(request: Request, body: AutoApproveRequest) -> dict[str, Any]:
    set_auto_approve(request, body.rule, body.enabled)
    return {"rule": body.rule, "auto_approve": body.enabled}


@router.get("/api/runs")
def api_runs(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    return [run_to_dict(r) for r in recent_runs(request, max(1, min(limit, 200)))]


@router.get("/api/runs/{run_id}")
def api_run(request: Request, run_id: int) -> dict[str, Any]:
    return run_to_dict(get_run(request, run_id), include_log=True)


class ScheduleRequest(BaseModel):
    name: str
    job: str
    cron: str
    timezone: str = "UTC"
    # dry by default: a live schedule has to be asked for
    dry_run: bool = True
    enabled: bool = True


def api_save_schedule(
    request: Request, schedule_id: int | None, body: ScheduleRequest
) -> JSONResponse:
    values = body.model_dump()
    values["name"] = values["name"].strip()
    values["cron"] = values["cron"].strip()
    values["timezone"] = values["timezone"].strip()
    if errors := schedule_errors(
        values["name"], values["job"], values["cron"], values["timezone"]
    ):
        return JSONResponse({"errors": errors}, status_code=422)
    saved_id = save_schedule(request, schedule_id, values)
    return JSONResponse(
        schedule_to_dict(get_schedule(request, saved_id), utc_now()),
        status_code=201 if schedule_id is None else 200,
    )


@router.get("/api/schedules")
def api_schedules(request: Request) -> list[dict[str, Any]]:
    now = utc_now()
    return [schedule_to_dict(s, now) for s in all_schedules(request)]


@router.post("/api/schedules", status_code=201)
def api_create_schedule(request: Request, body: ScheduleRequest) -> JSONResponse:
    return api_save_schedule(request, None, body)


@router.get("/api/schedules/{schedule_id}")
def api_schedule(request: Request, schedule_id: int) -> dict[str, Any]:
    return schedule_to_dict(get_schedule(request, schedule_id), utc_now())


@router.put("/api/schedules/{schedule_id}")
def api_update_schedule(
    request: Request, schedule_id: int, body: ScheduleRequest
) -> JSONResponse:
    get_schedule(request, schedule_id)
    return api_save_schedule(request, schedule_id, body)


@router.delete("/api/schedules/{schedule_id}", status_code=204)
def api_delete_schedule(request: Request, schedule_id: int) -> Response:
    delete_schedule(request, schedule_id)
    return Response(status_code=204)
