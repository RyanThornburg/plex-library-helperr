"""jobs.py - queue and run jobs one at a time on a worker thread, storing each run"""

import logging
import queue
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

import config
from config.models import ConfigSettings
from results import JOBS, JobResult
from web.db import JobRun

logger = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
# a runaway job can't fill the database
MAX_LOG_CHARS = 1_000_000

RunJob = Callable[[str, ConfigSettings], JobResult]
LoadConfig = Callable[[], ConfigSettings]


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AlreadyActive(Exception):
    """the job is already queued or running"""

    def __init__(self, run_id: int):
        super().__init__(f"run {run_id} is already active")
        self.run_id = run_id


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
    ):
        self.engine = engine
        self.load_config = load_config
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
        self, job: str, dry_run: bool, trigger: str, requested_by: str | None
    ) -> int:
        """queue a run and return its id, AlreadyActive if the job is queued/running"""
        if job not in JOBS:
            raise ValueError(f"unknown job '{job}'")
        with self._submit_lock, Session(self.engine) as session:
            active = session.scalar(
                select(JobRun.id).where(JobRun.job == job, JobRun.status.in_(ACTIVE))
            )
            if active is not None:
                raise AlreadyActive(active)
            run = JobRun(
                job=job,
                trigger=trigger,
                requested_by=requested_by,
                dry_run=dry_run,
                status="queued",
                created_at=utc_now(),
            )
            session.add(run)
            session.commit()
            run_id = run.id
        logger.info(
            "WEB\t Queued %s run %s (dry_run=%s) for %s", job, run_id, dry_run, requested_by
        )
        self._queue.put(run_id)
        return run_id

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
            job, dry_run = run.job, run.dry_run

        capture = RunLogCapture(threading.get_ident())
        root = logging.getLogger()
        root.addHandler(capture)
        result: JobResult | None = None
        try:
            logger.info("WEB\t Starting %s run %s (dry_run forced: %s)", job, run_id, dry_run)
            settings = self.load_config()
            if dry_run:
                config.force_dry_run(settings)
            result = self.run_job(job, settings)
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
