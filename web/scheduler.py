"""scheduler.py - queues scheduled runs on the JobRunner when their cron matches"""

import logging
import threading
import zoneinfo
from datetime import datetime

from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session

from config.models import WebDefaults
from results import JOBS
from web import cron
from web.db import JobRun, Schedule, TrashEvent
from web.jobs import ACTIVE, AlreadyActive, JobRunner, utc_now

logger = logging.getLogger(__name__)

# the columns' lengths
MAX_NAME = 100
MAX_CRON = 100

ZONES = sorted(zoneinfo.available_timezones())

# after a live movers/removers run moves or removes files, or radarr/sonarr upgrade one (a
# TrashEvent from the webhook), empty plex's trash once autoscan (minimum-age) and plex
# have scanned the folders: WebDefaults.followup_delay after the last of them, tried
# followup_attempts times when blocked (a scan still running), for work within
# followup_lookback
TRASH_ACTIONS = ("move", "remove")
# a steady stream of upgrades keeps moving the last one, so the follow-up is due at most
# this many delays after the first
FOLLOWUP_MAX_WAIT = 4


def left_trash(run: JobRun) -> bool:
    """moved or removed files, which plex puts in its trash"""
    changes = (run.result or {}).get("changes", [])
    return any(c["action"] in TRASH_ACTIONS and c["status"] == "done" for c in changes)


def emptied(run: JobRun) -> bool:
    """an empty trash run with the config's dry run settings that nothing stopped"""
    changes = (run.result or {}).get("changes", [])
    return (
        run.job == "empty_trash"
        and run.status == "succeeded"
        and not run.dry_run
        and all(c["status"] in ("done", "dry_run") for c in changes)
    )


def trash_followup_due(
    session: Session, now: datetime, defaults: WebDefaults
) -> datetime | None:
    """when to queue a follow-up empty trash, None when none is needed or follow-ups are
    off. Worked out from run history and trash events alone, so a restart doesn't lose one"""
    if defaults.followup_attempts == 0:
        return None
    trash_runs = list(
        session.scalars(
            select(JobRun)
            .where(JobRun.job == "empty_trash")
            .order_by(JobRun.id.desc())
            .limit(50)
        )
    )
    if any(r.status in ACTIVE for r in trash_runs):
        return None
    delay = defaults.followup_delay
    since = now - defaults.followup_lookback
    # an empty trash only covers what plex had scanned when it started
    last_emptied = next((r for r in trash_runs if emptied(r)), None)
    if last_emptied and last_emptied.started_at and last_emptied.started_at - delay > since:
        since = last_emptied.started_at - delay
    runs = session.scalars(
        select(JobRun).where(
            JobRun.job.in_(("movers", "removers")),
            JobRun.dry_run.is_(False),
            JobRun.finished_at > since,
        )
    )
    events = session.scalars(select(TrashEvent.received_at).where(TrashEvent.received_at > since))
    work = sorted([*(r.finished_at for r in runs if left_trash(r) and r.finished_at), *events])
    if not work:
        return None
    followups = [r for r in trash_runs if r.trigger == "followup"]
    attempts = [r for r in followups if r.created_at > work[-1]]
    if len(attempts) >= defaults.followup_attempts:
        return None
    if attempts:
        return max(r.finished_at or r.created_at for r in attempts) + delay
    # work since the last follow-up starts over, and can't put it off forever
    last_followup = max((r.created_at for r in followups), default=None)
    first = next((w for w in work if last_followup is None or w > last_followup), work[-1])
    return min(work[-1] + delay, first + FOLLOWUP_MAX_WAIT * delay)


def schedule_errors(name: str, job: str, expression: str, zone: str) -> list[str]:
    """what's wrong with a schedule from the ui or api, empty when it can be saved"""
    errors = []
    if not name.strip():
        errors.append("Name is required")
    elif len(name.strip()) > MAX_NAME:
        errors.append(f"Name can be at most {MAX_NAME} characters")
    if job not in JOBS:
        errors.append(f"Unknown job '{job}'")
    try:
        cron.parse_zone(zone)
    except ValueError as e:
        errors.append(f"Time zone: {e}")
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
        zone = cron.parse_zone(schedule.timezone)
        return cron.parse(schedule.cron).next_after_utc(now, zone)
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
        """queue every schedule that's due and a follow-up empty trash when one is,
        returns the queued run ids"""
        queued = self._schedules(now)
        with Session(self.engine) as session:
            due = trash_followup_due(session, now, self.runner.defaults())
        if due is not None and due <= now:
            try:
                queued.append(self.runner.submit("empty_trash", False, "followup", None))
            except AlreadyActive:
                pass
        return queued

    def _schedules(self, now: datetime) -> list[int]:
        queued = []
        with Session(self.engine) as session:
            schedules = list(session.scalars(select(Schedule).where(Schedule.enabled)))
        for schedule in schedules:
            since = max(
                t for t in (schedule.last_fired_at, schedule.updated_at, self.started_at) if t
            )
            try:
                zone = cron.parse_zone(schedule.timezone)
                due = cron.parse(schedule.cron).next_after_utc(since, zone)
            except ValueError as e:
                logger.error("WEB\t Schedule %s can't be checked: %s", schedule.id, e)
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
