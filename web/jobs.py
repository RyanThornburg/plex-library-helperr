"""jobs.py - queue and run jobs one at a time on a worker thread, storing each run"""

import logging
import queue
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

import config
from config.models import ConfigSettings, WebDefaults
from logging_config import SecretFilter, redacted
from movie_remover import PlayLookup
from results import JOBS, JobResult
from web.db import JobRun, RemoverRuleSetting

logger = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
# a runaway job can't fill the database
MAX_LOG_CHARS = 1_000_000
# removals a reviewed plan can approve: not already done or failed
APPROVABLE = ("dry_run", "blocked")
# removers deletions empty trash recognizes, plex's trash is emptied well before this
REMOVED_FILES_MAX_AGE = timedelta(days=90)

# run_job(job, settings), run_job("removers", settings, approved) to apply a plan, or
# run_job("removers", settings, auto_approve=keys) for removers that skip review. Removers
# runs also get movie_plays, for radarr removers that check plays
RunJob = Callable[..., JobResult]
LoadConfig = Callable[[], ConfigSettings]
LoadDefaults = Callable[[], WebDefaults]
MoviePlays = Callable[[ConfigSettings], PlayLookup]


def no_config() -> ConfigSettings:
    raise ValueError("no config to run with")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AlreadyActive(Exception):
    """the job is already queued or running"""

    def __init__(self, run_id: int):
        super().__init__(f"run {run_id} is already active")
        self.run_id = run_id


class ApplyRefused(Exception):
    """a plan can't be applied, the message says why and is shown to the user"""


def removal_service(removal: dict[str, Any]) -> str:
    """SONARR or RADARR. Runs stored before radarr removers only had sonarr's"""
    return removal.get("service", "SONARR")


def removal_key(removal: dict[str, Any]) -> str:
    """how a planned removal is selected for approval: sonarr:<series id> or
    radarr:<movie id>. Runs stored before radarr removers called the id series_id"""
    media_id = removal.get("media_id", removal.get("series_id"))
    return f"{removal_service(removal).lower()}:{media_id}"


def approvable(run: JobRun) -> list[dict[str, Any]]:
    """the removals of a finished removers run that can be approved"""
    if run.job != "removers" or run.plan_run_id is not None or run.status in ACTIVE:
        return []
    removals = (run.result or {}).get("removals", [])
    return [r for r in removals if r["status"] in APPROVABLE]


def hide_in(value: Any) -> Any:
    """a stored result with the config's secrets redacted from every string"""
    if isinstance(value, str):
        return redacted(value)
    if isinstance(value, list):
        return [hide_in(v) for v in value]
    if isinstance(value, dict):
        return {k: hide_in(v) for k, v in value.items()}
    return value


class RunLogCapture(logging.Handler):
    """collects log records from the worker thread only, so request logs from other
    threads don't end up in a run's log"""

    def __init__(self, thread_id: int):
        super().__init__(logging.INFO)
        self.thread_id = thread_id
        self.addFilter(SecretFilter())
        self.lines: list[str] = []
        self.size = 0
        self.truncated = False
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.thread_id or self.truncated:
            return
        line = self.format(record)
        if self.size + len(line) > MAX_LOG_CHARS:
            self.truncated = True
            self.lines.append("... log truncated")
            return
        self.lines.append(line)
        self.size += len(line) + 1

    def text(self) -> str:
        return "\n".join(self.lines)


class JobRunner:
    """jobs run in submission order on one thread, so two jobs never touch the arrs at
    once. runs interrupted by a restart are marked failed, never resumed"""

    def __init__(
        self,
        engine: Engine,
        run_job: RunJob | None = None,
        load_config: LoadConfig = no_config,
        allow_apply: bool = False,
        defaults: LoadDefaults = WebDefaults,
        movie_plays: MoviePlays | None = None,
    ):
        self.engine = engine
        self.load_config = load_config
        # the config's web defaults (e.g. plan max age), read when needed
        self.defaults = defaults
        # REMOVERS_ALLOW_APPLY, set on the container where the ui can't change it
        self.allow_apply = allow_apply
        if run_job is None:
            from main import run_job as main_run_job

            run_job = main_run_job
        self.run_job = run_job
        if movie_plays is None:
            from web.stats import movie_play_lookup

            def movie_plays(settings: ConfigSettings) -> PlayLookup:
                return movie_play_lookup(engine, settings)

        # radarr removers' play history, from the plays table
        self.movie_plays = movie_plays
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._submit_lock = threading.Lock()
        # job -> (trigger, requested_by) to queue again when its running run finishes
        self._rerun: dict[str, tuple[str, str | None]] = {}
        self._thread = threading.Thread(target=self._work, name="job-runner", daemon=True)

    def start(self) -> None:
        with Session(self.engine) as session:
            interrupted = session.execute(
                update(JobRun)
                .where(JobRun.status.in_(ACTIVE))
                .values(
                    status="failed",
                    finished_at=utc_now(),
                    # active runs have no stored log yet, it is written when they finish
                    log="Interrupted: the app restarted before this run finished",
                )
            ).rowcount
            session.commit()
        if interrupted:
            logger.warning("WEB\t Marked %s interrupted runs as failed", interrupted)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._queue.put(None)
        self._thread.join(timeout)

    def wait_idle(self) -> None:
        """block until every submitted run has finished (tests)"""
        self._queue.join()

    def submit(
        self,
        job: str,
        dry_run: bool,
        trigger: str,
        requested_by: str | None,
        schedule_id: int | None = None,
    ) -> int:
        """queue a run and return its id, AlreadyActive if the job is queued/running"""
        if job not in JOBS:
            raise ValueError(f"unknown job '{job}'")
        with self._submit_lock, Session(self.engine) as session:
            run_id = self._queue_run(
                session,
                JobRun(
                    job=job,
                    trigger=trigger,
                    requested_by=requested_by,
                    dry_run=dry_run,
                    schedule_id=schedule_id,
                ),
            )
        logger.info(
            "WEB\t Queued %s run %s (dry_run=%s) for %s", job, run_id, dry_run, requested_by
        )
        return run_id

    def auto_approved_rules(self) -> frozenset[str]:
        """keys of removers that delete without review, none while applying is off"""
        if not self.allow_apply:
            return frozenset()
        with Session(self.engine) as session:
            return frozenset(
                session.scalars(
                    select(RemoverRuleSetting.rule_key).where(RemoverRuleSetting.auto_approve)
                )
            )

    def removed_files(self) -> dict[str, int]:
        """names of the files removers runs deleted lately, mapped to the run. Names, not
        paths: sonarr and plex see the same file under different paths"""
        since = utc_now() - REMOVED_FILES_MAX_AGE
        files: dict[str, int] = {}
        with Session(self.engine) as session:
            runs = session.execute(
                select(JobRun.id, JobRun.result)
                .where(JobRun.job == "removers", JobRun.finished_at >= since)
                .order_by(JobRun.id)
            )
            for run_id, result in runs:
                for removal in (result or {}).get("removals", []):
                    if removal["status"] == "done":
                        for file in removal["files"]:
                            files[PurePosixPath(file["path"]).name] = run_id
        return files

    def submit_soon(self, job: str, trigger: str, requested_by: str | None) -> int | None:
        """queue a run with the config's dry run settings unless one is already queued, which will
        see whatever asked for this. While one is running, one more is queued when it
        finishes. Returns the queued run's id, None when it'll follow the running one"""
        with self._submit_lock, Session(self.engine) as session:
            active = session.execute(
                select(JobRun.id, JobRun.status).where(
                    JobRun.job == job, JobRun.status.in_(ACTIVE)
                )
            ).first()
            if active is not None and active.status == "queued":
                return active.id
            if active is not None:
                self._rerun[job] = (trigger, requested_by)
                return None
            run = JobRun(job=job, trigger=trigger, requested_by=requested_by, dry_run=False)
            run_id = self._queue_run(session, run)
        logger.info("WEB\t Queued %s run %s for %s", job, run_id, requested_by or trigger)
        return run_id

    def apply_problem(self, session: Session, plan: JobRun) -> str | None:
        """why a removers run's plan can't be applied, None when it can"""
        if not self.allow_apply:
            return (
                "Applying plans is turned off. Set REMOVERS_ALLOW_APPLY=true on the "
                "container to allow it."
            )
        if not approvable(plan):
            return "This run has no planned removals to approve."
        max_age = self.defaults().plan_max_age
        if utc_now() - plan.created_at > max_age:
            return (
                f"This plan is older than {config.format_duration(max_age)} (Settings → "
                "General). Run removers again for a fresh plan."
            )
        applied = session.scalar(select(JobRun.id).where(JobRun.plan_run_id == plan.id))
        if applied is not None:
            return f"This plan was already applied by run {applied}."
        return None

    def submit_apply(
        self, plan_run_id: int, keys: list[str], trigger: str, requested_by: str | None
    ) -> int:
        """queue a live removers run that removes the approved series/movies of a plan,
        selected by removal_key(). Raises ApplyRefused, AlreadyActive, or LookupError when
        there's no such run"""
        with self._submit_lock, Session(self.engine) as session:
            plan = session.get(JobRun, plan_run_id)
            if plan is None:
                raise LookupError(plan_run_id)
            if problem := self.apply_problem(session, plan):
                raise ApplyRefused(problem)
            removals = {removal_key(r): r for r in approvable(plan)}
            chosen = set(keys)
            if not chosen:
                raise ApplyRefused("Select at least one series or movie to remove.")
            if chosen - removals.keys():
                raise ApplyRefused("Some selected series or movies aren't in this plan.")
            approved = [
                {
                    "service": removal_service(removals[key]),
                    "media_id": int(key.split(":", 1)[1]),
                    "title": removals[key]["title"],
                    "file_ids": [f["id"] for f in removals[key]["files"]],
                }
                for key in sorted(chosen)
            ]
            run_id = self._queue_run(
                session,
                JobRun(
                    job="removers",
                    trigger=trigger,
                    requested_by=requested_by,
                    dry_run=False,
                    plan_run_id=plan_run_id,
                    approved=approved,
                ),
            )
        logger.info(
            "WEB\t Queued run %s applying %s removals of plan %s for %s",
            run_id,
            len(approved),
            plan_run_id,
            requested_by,
        )
        return run_id

    def _queue_run(self, session: Session, run: JobRun) -> int:
        """add a run and queue it, the caller holds _submit_lock"""
        active = session.scalar(
            select(JobRun.id).where(JobRun.job == run.job, JobRun.status.in_(ACTIVE))
        )
        if active is not None:
            raise AlreadyActive(active)
        run.status = "queued"
        run.created_at = utc_now()
        session.add(run)
        session.commit()
        self._queue.put(run.id)
        return run.id

    def _work(self) -> None:
        while True:
            run_id = self._queue.get()
            try:
                if run_id is None:
                    return
                try:
                    job = self._execute(run_id)
                except Exception:
                    # e.g. the db stayed locked: the run fails, the jobs after it still run
                    logger.exception("WEB\t Run %s couldn't be run or stored", run_id)
                    self._fail(run_id)
                    continue
                with self._submit_lock:
                    rerun = self._rerun.pop(job, None)
                if rerun:
                    try:
                        self.submit(job, False, *rerun)
                    except AlreadyActive:
                        pass  # queued meanwhile, that run covers it
            finally:
                self._queue.task_done()

    def _fail(self, run_id: int) -> None:
        try:
            with Session(self.engine) as session:
                run = session.get_one(JobRun, run_id)
                run.status, run.finished_at = "failed", utc_now()
                session.commit()
        except Exception:
            logger.exception("WEB\t Run %s couldn't be marked failed", run_id)

    def _execute(self, run_id: int) -> str:
        """run it and store the result, returns the job"""
        with Session(self.engine) as session:
            run = session.get_one(JobRun, run_id)
            run.status = "running"
            run.started_at = utc_now()
            session.commit()
            job, dry_run, approved = run.job, run.dry_run, run.approved

        capture = RunLogCapture(threading.get_ident())
        root = logging.getLogger()
        root.addHandler(capture)
        result: JobResult | None = None
        try:
            logger.info("WEB\t Starting %s run %s (dry_run forced: %s)", job, run_id, dry_run)
            settings = self.load_config()
            if dry_run:
                config.force_dry_run(settings)
            # only what a run needs is passed, plain runs are run_job(job, settings)
            kwargs: dict[str, Any] = {}
            if approved is not None:
                # only submit_apply queues these, checked again in case anything else does
                if not self.allow_apply:
                    raise ApplyRefused("REMOVERS_ALLOW_APPLY is off, not applying the plan")
                by_service: dict[str, dict[int, set[int]]] = {}
                for a in approved:
                    by_service.setdefault(a["service"], {})[a["media_id"]] = set(a["file_ids"])
                kwargs["approved"] = by_service
            elif job == "removers" and not dry_run:
                # a forced dry run never deletes, so auto-approve only counts without one
                if auto := self.auto_approved_rules():
                    logger.info(
                        "WEB\t Auto-approve on for removers %s", "; ".join(sorted(auto))
                    )
                    kwargs["auto_approve"] = auto
            if job == "removers" and settings.radarr and settings.radarr.removers:
                kwargs["movie_plays"] = self.movie_plays(settings)
            if job == "empty_trash" and (removed := self.removed_files()):
                kwargs["removed_files"] = removed
            result = self.run_job(job, settings, **kwargs)
            result.log_summary()
        except Exception:
            # any failure, including a broken config, ends up in the run's log
            logger.exception("WEB\t %s run %s failed", job, run_id)
        finally:
            root.removeHandler(capture)

        with Session(self.engine) as session:
            run = session.get_one(JobRun, run_id)
            run.status = "succeeded" if result is not None and not result.failed else "failed"
            run.finished_at = utc_now()
            run.result = hide_in(result.to_dict()) if result is not None else None
            run.log = capture.text()
            session.commit()
        return job
