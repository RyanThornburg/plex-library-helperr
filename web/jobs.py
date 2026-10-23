"""jobs.py - queue and run jobs one at a time on a worker thread, storing each run"""

import logging
import queue
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

import config
from config.models import ConfigSettings
from results import JOBS, JobResult
from web.db import JobRun, RemoverRuleSetting

logger = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
# a runaway job can't fill the database
MAX_LOG_CHARS = 1_000_000
# removals a reviewed plan can approve: not already done or failed
APPROVABLE = ("dry_run", "blocked")
# an older plan has to be made again, it's been too long since it was reviewed
PLAN_MAX_AGE = timedelta(days=7)

# run_job(job, settings), run_job("removers", settings, approved) to apply a plan, or
# run_job("removers", settings, auto_approve=keys) for removers that skip review
RunJob = Callable[..., JobResult]
LoadConfig = Callable[[], ConfigSettings]


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AlreadyActive(Exception):
    """the job is already queued or running"""

    def __init__(self, run_id: int):
        super().__init__(f"run {run_id} is already active")
        self.run_id = run_id


class ApplyRefused(Exception):
    """a plan can't be applied, the message says why and is shown to the user"""


def approvable(run: JobRun) -> list[dict[str, Any]]:
    """the removals of a finished removers run that can be approved"""
    if run.job != "removers" or run.plan_run_id is not None or run.status in ACTIVE:
        return []
    removals = (run.result or {}).get("removals", [])
    return [r for r in removals if r["status"] in APPROVABLE]


class RunLogCapture(logging.Handler):
    """collects log records from the worker thread only, so request logs from other
    threads don't end up in a run's log"""

    def __init__(self, thread_id: int):
        super().__init__(logging.INFO)
        self.thread_id = thread_id
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
        load_config: LoadConfig = config.load_config,
        allow_apply: bool = False,
    ):
        self.engine = engine
        self.load_config = load_config
        # REMOVERS_ALLOW_APPLY, set on the container where the ui can't change it
        self.allow_apply = allow_apply
        if run_job is None:
            from main import run_job as main_run_job

            run_job = main_run_job
        self.run_job = run_job
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._submit_lock = threading.Lock()
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

    def apply_problem(self, session: Session, plan: JobRun) -> str | None:
        """why a removers run's plan can't be applied, None when it can"""
        if not self.allow_apply:
            return (
                "Applying plans is turned off. Set REMOVERS_ALLOW_APPLY=true on the "
                "container to allow it."
            )
        if not approvable(plan):
            return "This run has no planned removals to approve."
        if utc_now() - plan.created_at > PLAN_MAX_AGE:
            return (
                f"This plan is more than {PLAN_MAX_AGE.days} days old. Run removers again "
                "for a fresh plan."
            )
        applied = session.scalar(select(JobRun.id).where(JobRun.plan_run_id == plan.id))
        if applied is not None:
            return f"This plan was already applied by run {applied}."
        return None

    def submit_apply(
        self, plan_run_id: int, series_ids: list[int], trigger: str, requested_by: str | None
    ) -> int:
        """queue a live removers run that removes the approved series of a plan, raises
        ApplyRefused, AlreadyActive, or LookupError when there's no such run"""
        with self._submit_lock, Session(self.engine) as session:
            plan = session.get(JobRun, plan_run_id)
            if plan is None:
                raise LookupError(plan_run_id)
            if problem := self.apply_problem(session, plan):
                raise ApplyRefused(problem)
            removals = {r["series_id"]: r for r in approvable(plan)}
            chosen = set(series_ids)
            if not chosen:
                raise ApplyRefused("Select at least one series to remove.")
            if chosen - removals.keys():
                raise ApplyRefused("Some selected series aren't in this plan.")
            approved = [
                {
                    "series_id": series_id,
                    "title": removals[series_id]["title"],
                    "file_ids": [f["id"] for f in removals[series_id]["files"]],
                }
                for series_id in sorted(chosen)
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
            "WEB\t Queued run %s applying %s series of plan %s for %s",
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
                self._execute(run_id)
            finally:
                self._queue.task_done()

    def _execute(self, run_id: int) -> None:
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
            # a forced dry run never deletes, so auto-approve only counts without one
            plain_live_removers = job == "removers" and not dry_run and approved is None
            auto = self.auto_approved_rules() if plain_live_removers else None
            if approved is None and auto:
                logger.info("WEB\t Auto-approve on for removers %s", "; ".join(sorted(auto)))
                result = self.run_job(job, settings, auto_approve=auto)
            elif approved is None:
                result = self.run_job(job, settings)
            else:
                # only submit_apply queues these, checked again in case anything else does
                if not self.allow_apply:
                    raise ApplyRefused("REMOVERS_ALLOW_APPLY is off, not applying the plan")
                result = self.run_job(
                    job,
                    settings,
                    {a["series_id"]: set(a["file_ids"]) for a in approved},
                )
            result.log_summary()
        except Exception:
            # any failure, including a broken config.toml, ends up in the run's log
            logger.exception("WEB\t %s run %s failed", job, run_id)
        finally:
            root.removeHandler(capture)

        with Session(self.engine) as session:
            run = session.get_one(JobRun, run_id)
            run.status = "succeeded" if result is not None and not result.failed else "failed"
            run.finished_at = utc_now()
            run.result = result.to_dict() if result is not None else None
            run.log = capture.text()
            session.commit()
