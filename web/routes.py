"""routes.py - pages and json api for running jobs and reviewing runs"""

import pathlib
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from results import JOBS
from web.db import JobRun
from web.jobs import ACTIVE, AlreadyActive, JobRunner

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


templates.env.filters["localtime"] = localtime
templates.env.globals["duration"] = duration
templates.env.globals["JOB_INFO"] = JOB_INFO


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
    }
    if include_log:
        data["log"] = run.log
    return data


def check_job(job: str) -> None:
    if job not in JOBS:
        raise HTTPException(status_code=404)


# ---------------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    # read fresh each time, so the page reflects config.toml edits without a restart
    dry_runs: dict[str, bool | None] = {}
    config_error = None
    try:
        settings = request.app.state.load_config()
        dry_runs = {job: config.config_dry_run(job, settings) for job in JOBS}
    except Exception as e:  # shown on the page, jobs would fail the same way
        config_error = f"{type(e).__name__}: {e}"
    runs = recent_runs(request, 25)
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
        },
    )


@router.get("/jobs/{job}/confirm", response_class=HTMLResponse)
def confirm_page(request: Request, job: str) -> HTMLResponse:
    """runs with config.toml's dry_run go through this page, it says whether they're live"""
    check_job(job)
    mode: bool | None = None
    config_error = None
    try:
        mode = config.config_dry_run(job, request.app.state.load_config())
    except Exception as e:
        config_error = f"{type(e).__name__}: {e}"
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
