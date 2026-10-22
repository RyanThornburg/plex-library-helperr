"""routes.py - pages and json api for running jobs, reviewing runs and scheduling jobs"""

import pathlib
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import config
from results import JOBS
from web.db import JobRun, Schedule
from web.jobs import ACTIVE, AlreadyActive, JobRunner, utc_now
from web.scheduler import next_run_at, schedule_errors

router = APIRouter()

templates = Jinja2Templates(directory=pathlib.Path(__file__).parent / "templates")

JOB_INFO: dict[str, tuple[str, str]] = {
    "movers": (
        "Movers & profilers",
        "Move media to the root folders in your mover rules and set quality profiles.",
    ),
    "removers": (
        "Sonarr removers",
        "Unmonitor and delete old seasons/episodes of tagged series.",
    ),
    "empty_trash": (
        "Empty Plex trash",
        "Clear trashed items, skipping libraries that are scanning or over max_items.",
    ),
}


def localtime(value: datetime | None) -> str:
    """stored times are naive utc, shown in the container's TZ"""
    if value is None:
        return "-"
    return value.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def duration(run: JobRun) -> str:
    if not run.started_at or not run.finished_at:
        return "-"
    seconds = int((run.finished_at - run.started_at).total_seconds())
    return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"


def tz_name() -> str:
    """the container's TZ, which schedules run in"""
    return datetime.now().astimezone().tzname() or "local time"


def run_source(run: JobRun) -> str:
    if run.requested_by:
        return run.requested_by
    if run.schedule:
        return f"schedule: {run.schedule.name}"
    return run.trigger


templates.env.filters["localtime"] = localtime
templates.env.globals["duration"] = duration
templates.env.globals["run_source"] = run_source
templates.env.globals["tz_name"] = tz_name
templates.env.globals["JOB_INFO"] = JOB_INFO

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


def recent_runs(request: Request, limit: int) -> list[JobRun]:
    with Session(request.app.state.engine) as session:
        return list(
            session.scalars(select(JobRun).order_by(JobRun.id.desc()).limit(limit))
        )


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
    }
    if include_log:
        data["log"] = run.log
    return data


def check_job(job: str) -> None:
    if job not in JOBS:
        raise HTTPException(status_code=404)


def config_modes(request: Request) -> tuple[dict[str, bool | None], str | None]:
    """each job's dry run state in config.toml, read fresh so edits show without a restart"""
    try:
        settings = request.app.state.load_config()
        return {job: config.config_dry_run(job, settings) for job in JOBS}, None
    except Exception as e:  # shown on the page, jobs would fail the same way
        return {}, f"{type(e).__name__}: {e}"


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
        "dry_run": schedule.dry_run,
        "enabled": schedule.enabled,
        "created_at": schedule.created_at.isoformat() + "Z",
        "updated_at": schedule.updated_at.isoformat() + "Z",
        "last_fired_at": (
            schedule.last_fired_at.isoformat() + "Z" if schedule.last_fired_at else None
        ),
        "next_run_at": next_run.isoformat() + "Z" if next_run else None,
    }


async def form_fields(request: Request) -> dict[str, str]:
    """a urlencoded form body, without python-multipart. Unchecked boxes are absent"""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise HTTPException(status_code=413)
    fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[-1] for key, values in fields.items()}


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    dry_runs, config_error = config_modes(request)
    runs = recent_runs(request, 25)
    # each job's soonest scheduled run
    now = utc_now()
    next_runs: dict[str, tuple[datetime, Schedule]] = {}
    for schedule in all_schedules(request):
        next_run = next_run_at(schedule, now)
        if next_run and (schedule.job not in next_runs or next_run < next_runs[schedule.job][0]):
            next_runs[schedule.job] = (next_run, schedule)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": request.state.user,
            "runs": runs,
            "jobs": JOBS,
            "dry_runs": dry_runs,
            "config_error": config_error,
            "active_jobs": {r.job for r in runs if r.status in ACTIVE},
            "next_runs": next_runs,
        },
    )


@router.get("/jobs/{job}/confirm", response_class=HTMLResponse)
def confirm_page(request: Request, job: str) -> HTMLResponse:
    """runs with config.toml's dry_run go through this page, it says whether they're live"""
    check_job(job)
    dry_runs, config_error = config_modes(request)
    mode = dry_runs.get(job)
    return templates.TemplateResponse(
        request,
        "confirm.html",
        {"user": request.state.user, "job": job, "mode": mode, "config_error": config_error},
    )


@router.post("/jobs/{job}")
def run_job_form(request: Request, job: str, dry_run: bool = True) -> RedirectResponse:
    """the page's buttons post here with ?dry_run=..., no request body to parse"""
    check_job(job)
    try:
        run_id = runner(request).submit(job, dry_run, "manual", request.state.user)
    except AlreadyActive as e:
        run_id = e.run_id
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: int) -> HTMLResponse:
    run = get_run(request, run_id)
    return templates.TemplateResponse(
        request,
        "run.html",
        {"user": request.state.user, "run": run, "active": run.status in ACTIVE},
    )


def schedule_form(
    request: Request,
    schedule_id: int | None,
    values: dict[str, Any],
    errors: list[str] | None = None,
) -> HTMLResponse:
    dry_runs, config_error = config_modes(request)
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
            "next_run": next_run,
        },
        status_code=400 if errors else 200,
    )


def form_values(fields: dict[str, str]) -> dict[str, Any]:
    return {
        "name": fields.get("name", "").strip(),
        "job": fields.get("job", ""),
        "cron": fields.get("cron", "").strip(),
        "dry_run": "dry_run" in fields,
        "enabled": "enabled" in fields,
    }


def schedule_values(schedule: Schedule) -> dict[str, Any]:
    return {
        "name": schedule.name,
        "job": schedule.job,
        "cron": schedule.cron,
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
    # dry by default, like the api: a live schedule has to be asked for
    values = {"name": "", "job": JOBS[0], "cron": "", "dry_run": True, "enabled": True}
    return schedule_form(request, None, values)


@router.post("/schedules")
async def create_schedule_form(request: Request) -> Response:
    values = form_values(await form_fields(request))
    if errors := schedule_errors(values["name"], values["job"], values["cron"]):
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
    if errors := schedule_errors(values["name"], values["job"], values["cron"]):
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
    # dry by default: a live schedule has to be asked for
    dry_run: bool = True
    enabled: bool = True


def api_save_schedule(
    request: Request, schedule_id: int | None, body: ScheduleRequest
) -> JSONResponse:
    values = body.model_dump()
    values["name"] = values["name"].strip()
    values["cron"] = values["cron"].strip()
    if errors := schedule_errors(values["name"], values["job"], values["cron"]):
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
