"""scheduler.py - queues scheduled runs on the JobRunner when their cron matches"""

import logging
import threading
from datetime import datetime

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from results import JOBS
from web import cron
from web.db import Schedule
from web.jobs import AlreadyActive, JobRunner, utc_now

logger = logging.getLogger(__name__)

# the columns' lengths
MAX_NAME = 100
MAX_CRON = 100


def schedule_errors(name: str, job: str, expression: str) -> list[str]:
    """what's wrong with a schedule from the ui or api, empty when it can be saved"""
    errors = []
    if not name.strip():
        errors.append("Name is required")
    elif len(name.strip()) > MAX_NAME:
        errors.append(f"Name can be at most {MAX_NAME} characters")
    if job not in JOBS:
        errors.append(f"Unknown job '{job}'")
    if len(expression) > MAX_CRON:
        errors.append(f"Schedule can be at most {MAX_CRON} characters")
        return errors
    try:
        cron.parse(expression)
    except ValueError as e:
        errors.append(f"Schedule: {e}")
    return errors


def next_run_at(schedule: Schedule, now: datetime) -> datetime | None:
    """when an enabled schedule next fires (naive utc), for display"""
    if not schedule.enabled:
        return None
    try:
        return cron.parse(schedule.cron).next_after_utc(now)
    except ValueError:
        return None


class Scheduler:
    """checks enabled schedules at the start of every minute. A schedule fires at most
    once for the times it missed, and never for times before the app started or it was
    last edited, so a restart or edit doesn't set off a burst of runs"""

    def __init__(self, engine: Engine, runner: JobRunner):
        self.engine = engine
        self.runner = runner
        self.started_at = utc_now()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)

    def start(self) -> None:
        self.started_at = utc_now()
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = utc_now()
            try:
                self.tick(now)
            except Exception:
                logger.exception("WEB\t Scheduler check failed")
            # a second into the next minute, so a cron minute has always started
            self._stop.wait(61 - now.second - now.microsecond / 1_000_000)

    def tick(self, now: datetime) -> list[int]:
        """queue every schedule that's due, returns the queued run ids"""
        queued = []
        with Session(self.engine) as session:
            schedules = list(session.scalars(select(Schedule).where(Schedule.enabled)))
        for schedule in schedules:
            since = max(
                t for t in (schedule.last_fired_at, schedule.updated_at, self.started_at) if t
            )
            try:
                due = cron.parse(schedule.cron).next_after_utc(since)
            except ValueError as e:
                logger.error("WEB\t Schedule %s has a bad cron: %s", schedule.id, e)
                continue
            if due is None or due > now:
                continue
            # recorded before queuing, so a failure can never fire it twice
            with Session(self.engine) as session:
                session.execute(
                    update(Schedule)
                    .where(Schedule.id == schedule.id)
                    .values(last_fired_at=now)
                )
                session.commit()
            try:
                queued.append(
                    self.runner.submit(
                        schedule.job, schedule.dry_run, "schedule", None, schedule.id
                    )
                )
            except AlreadyActive as e:
                logger.warning(
                    "WEB\t Schedule '%s' skipped, %s run %s is still active",
                    schedule.name,
                    schedule.job,
                    e.run_id,
                )
            except ValueError as e:
                logger.error("WEB\t Schedule '%s' can't run: %s", schedule.name, e)
        return queued
