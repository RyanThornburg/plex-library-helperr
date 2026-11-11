"""queue_cleanup.py - the clear_stuck job: removes downloads stuck in radarr's/sonarr's
queue ([radarr.queue_cleanup], [sonarr.queue_cleanup]) once the health checks have seen
them stuck for stuck_for, like the queue page's Remove. How long a download has been
stuck is its health incident's age (web.health, keyed by the download client's id), the
same "for 3h" the Status page shows. The health check queues a run when one comes due
(due_services()); a run started by hand or a schedule acts on everything due"""

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from config.models import ArrConfig, ConfigSettings, QueueCleanup
from logging_config import failure_text
from results import Change, JobResult, utc_now
from web.db import HealthIncident
from web.health import item_key, span
from web.status import ArrQueue, Item, ServiceStatus, StuckDownload

logger = logging.getLogger(__name__)

# (config section, label, log name)
ARRS = (("radarr", "Radarr", "RADARR"), ("sonarr", "Sonarr", "SONARR"))
# the arrs' health checks (their `source`) that block imports for reasons that aren't
# the download's: a missing root folder or mount, a download client that can't be
# reached, a wrong remote path mapping. Cleanup removes nothing while one is reported
BLOCKING_CHECKS = {
    "RootFolderCheck",
    "MountCheck",
    "RemotePathMappingCheck",
    "DownloadClientCheck",
    "DownloadClientStatusCheck",
    "DownloadClientRootFolderCheck",
}
METHOD_LABELS = {"client": "remove from the download client", "ignore": "remove from the queue only"}
BLOCKLIST_LABELS = {"none": "don't blocklist", "search": "blocklist and search", "only": "blocklist only"}


class StuckClock(Protocol):
    """when the health checks first saw each download stuck, and what cleanup did"""

    def since(self, service: str, keys: list[str]) -> dict[str, datetime]: ...

    def mark(self, service: str, keys: list[str], status: str, now: datetime) -> None: ...


def incident_key(download_key: str) -> str:
    """a download's health incident key, as web.health.item_key makes it"""
    return item_key(Item("", key=download_key))


class IncidentClock:
    """StuckClock from the open health incidents"""

    def __init__(self, engine: Engine):
        self.engine = engine

    def since(self, service: str, keys: list[str]) -> dict[str, datetime]:
        by_incident = {incident_key(k): k for k in keys}
        with Session(self.engine) as session:
            rows = session.execute(
                select(HealthIncident.key, HealthIncident.started_at).where(
                    HealthIncident.service == service,
                    HealthIncident.key.in_(by_incident),
                    HealthIncident.ended_at.is_(None),
                )
            )
            return {by_incident[key]: started for key, started in rows}

    def mark(self, service: str, keys: list[str], status: str, now: datetime) -> None:
        with Session(self.engine) as session:
            session.execute(
                update(HealthIncident)
                .where(
                    HealthIncident.service == service,
                    HealthIncident.key.in_([incident_key(k) for k in keys]),
                    HealthIncident.ended_at.is_(None),
                )
                .values(cleanup=status, cleanup_at=now)
            )
            session.commit()


def removable(states: Iterable[str], key: str, cleanup: QueueCleanup) -> bool:
    """a stuck download cleanup may remove: every record in a state it's set to remove,
    and a download client id, which its age is kept by (one without is keyed by title)"""
    states = set(states)
    return bool(key) and not key.startswith("queue:") and bool(states) and states <= cleanup.tracked_states()


def due(
    downloads: list[StuckDownload], cleanup: QueueCleanup, since: dict[str, datetime], now: datetime
) -> list[tuple[StuckDownload, datetime]]:
    """the removable downloads stuck for at least stuck_for, with when they got stuck. A
    download the health checks haven't seen yet isn't due"""
    return [
        (d, since[d.key])
        for d in downloads
        if removable(d.states, d.key, cleanup) and d.key in since and now - since[d.key] >= cleanup.stuck_for
    ]


def blocking_problems(health: list[dict[str, object]]) -> list[str]:
    """the arr's health messages that mean imports fail whatever the download"""
    return [
        str(check.get("message") or check.get("source"))
        for check in health
        if check.get("source") in BLOCKING_CHECKS and check.get("type") in ("warning", "error")
    ]


def download_label(download: StuckDownload, stuck_since: datetime, now: datetime) -> str:
    title = download.titles[0] + (f" and {len(download.titles) - 1} more" if len(download.titles) > 1 else "")
    return f"{title}, stuck {span(now - stuck_since)}: {download.detail}"


def action_label(cleanup: QueueCleanup) -> str:
    return f"{METHOD_LABELS[cleanup.method]}, {BLOCKLIST_LABELS[cleanup.blocklist]}"


def clean_arr(
    service: str,
    log_name: str,
    arr: ArrConfig,
    queue: ArrQueue,
    clock: StuckClock,
    result: JobResult,
    now: datetime,
) -> None:
    cleanup = arr.queue_cleanup
    stuck = queue.stuck()
    candidates = [d for d in stuck if removable(d.states, d.key, cleanup)]
    since = clock.since(service, [d.key for d in candidates]) if candidates else {}
    found = due(candidates, cleanup, since, now)
    for download in stuck:
        if download not in candidates:
            logger.info("%s\t Not removing %s: %s isn't set to be removed", log_name, download.titles[0], "/".join(download.states) or "its state")
        elif download.key not in since:
            logger.info("%s\t Not removing %s yet: the health check hasn't seen it stuck", log_name, download.titles[0])
        elif all(download is not d for d, _ in found):
            logger.info("%s\t Not removing %s yet: stuck %s", log_name, download.titles[0], span(now - since[download.key]))
    if not found:
        logger.info("%s\t Nothing stuck for %s", log_name, span(cleanup.stuck_for))
        return

    items = [download_label(d, started, now) for d, started in found]
    keys = [d.key for d, _ in found]
    target = action_label(cleanup)

    def record(status: str, detail: str = "") -> None:
        result.changes.append(Change(log_name, "remove_download", target, items, status, detail))  # type: ignore[arg-type]
        clock.mark(service, keys, status, now)

    # before the dry run too, so it says what a live run would do
    if problems := blocking_problems(queue.health()):
        logger.warning("%s\t Not removing %s stuck downloads, health check: %s", log_name, len(found), "; ".join(problems))
        record("blocked", "Imports may be failing for every download, none removed until it clears: " + "; ".join(problems))
        return
    if cleanup.max_per_run and len(found) > cleanup.max_per_run:
        logger.warning("%s\t %s stuck downloads are due, over max_per_run %s: none removed", log_name, len(found), cleanup.max_per_run)
        record("blocked", f"{len(found)} downloads are due, over the max per run ({cleanup.max_per_run}), so none were removed")
        return
    if cleanup.dry_run:
        for item in items:
            logger.info("%s\t [DRY RUN] Would %s: %s", log_name, target, item)
        record("dry_run")
        return
    try:
        queue.remove([i for d, _ in found for i in d.ids], cleanup.method, cleanup.blocklist)
    except Exception as e:
        text = failure_text(e)
        logger.error("%s\t Removing %s stuck downloads failed: %s", log_name, len(found), text)
        record("failed", text)
        return
    for item in items:
        logger.info("%s\t Removed (%s): %s", log_name, target, item)
    record("done")


def run_queue_cleanup(
    settings: ConfigSettings,
    result: JobResult,
    clock: StuckClock | None,
    now: datetime | None = None,
    queue_for: Callable[[str, str, ArrConfig], ArrQueue] = ArrQueue,
) -> None:
    """removes what's due in each arr with queue cleanup on. One arr failing doesn't stop
    the other"""
    now = now or utc_now()
    for service, label, log_name in ARRS:
        arr: ArrConfig | None = getattr(settings, service)
        if arr is None or not arr.queue_cleanup.enabled:
            continue
        logger.info("-" * 90)
        logger.info("STARTING %s QUEUE CLEANUP", log_name)
        if not arr.baseurl or not arr.apikey:
            logger.warning("%s\t Not connected, skipping", log_name)
            continue
        if clock is None:
            # the command line has no health checks
            logger.warning("%s\t How long downloads have been stuck is only known to the web app, skipping", log_name)
            continue
        try:
            clean_arr(service, log_name, arr, queue_for(service, label, arr), clock, result, now)
        except Exception as e:
            text = failure_text(e)
            logger.error("%s\t Queue cleanup failed: %s", log_name, text)
            result.errors.append(f"{label} queue cleanup failed: {text}")


def acts_again(done: str | None, done_at: datetime | None, cleanup: QueueCleanup, now: datetime) -> bool:
    """whether a due download needs a run, given what cleanup last did with it"""
    if done is None or done_at is None:
        return True
    if done == "dry_run":
        return not cleanup.dry_run
    # blocked by max_per_run or the arr's health, or failed: try again later, not every
    # health check
    return done in ("blocked", "failed") and now - done_at >= cleanup.stuck_for


STATE_NAMES = {"failed": "failed", "failedPending": "failed", "importBlocked": "import blocked", "importPending": "import pending"}


def summary(cleanup: QueueCleanup) -> str:
    """what cleanup does, for the queue page"""
    text = f"after {span(cleanup.stuck_for)} stuck, {action_label(cleanup)}"
    if cleanup.max_per_run:
        text += f", at most {cleanup.max_per_run} a run"
    return text + (" (dry run: lists them, removes nothing)" if cleanup.dry_run else "")


def auto_note(download: StuckDownload, cleanup: QueueCleanup, stuck_since: datetime | None, now: datetime) -> str:
    """when cleanup will get to a stuck download, for the queue page"""
    if not removable(download.states, download.key, cleanup):
        states = sorted({STATE_NAMES.get(s, s) for s in download.states if s})
        return "Not removed automatically" + (f": {', '.join(states)}" if states else "")
    verb = "Listed by a dry run" if cleanup.dry_run else "Removed automatically"
    if stuck_since is None:
        return f"{verb} {span(cleanup.stuck_for)} after the health check first sees it"
    left = cleanup.stuck_for - (now - stuck_since)
    if left > timedelta(0):
        return f"{verb} in {span(left)}"
    return f"Due: stuck {span(now - stuck_since)}"


def due_services(engine: Engine, settings: ConfigSettings, statuses: Iterable[ServiceStatus], now: datetime) -> list[str]:
    """the arrs with a stuck download that has come due and cleanup hasn't acted on yet,
    only did a dry run of while it's live now, or blocked/failed on at least stuck_for
    ago. The health check queues a run for them"""
    found = []
    with Session(engine) as session:
        for status in statuses:
            arr = getattr(settings, status.service, None) if status.service in ("radarr", "sonarr") else None
            if arr is None or not arr.queue_cleanup.enabled or status.error:
                continue
            cleanup = arr.queue_cleanup
            keys = [item_key(i) for i in status.items if removable(i.states, i.key, cleanup)]
            if not keys:
                continue
            rows = session.execute(
                select(HealthIncident.started_at, HealthIncident.cleanup, HealthIncident.cleanup_at).where(
                    HealthIncident.service == status.service,
                    HealthIncident.key.in_(keys),
                    HealthIncident.ended_at.is_(None),
                )
            )
            if any(
                now - started >= cleanup.stuck_for and acts_again(done, done_at, cleanup, now)
                for started, done, done_at in rows
            ):
                found.append(status.label)
    return found
