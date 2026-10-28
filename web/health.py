"""health.py - health checks: the status page's checks every CHECK_INTERVAL, with each
problem kept as an incident from when it was first seen to when it cleared, so problems
show even when nobody had the status page open.

A problem is the same from one check to the next by its key (Problem.key, or its text),
so a count or countdown in the text doesn't start a new incident, and one that clears
and comes back within REOPEN_WITHIN continues the same incident. A service that can't
be checked gets a "down" incident and its other incidents stay as they were: nobody
knows whether they cleared"""

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from config.models import ConfigSettings
from web.db import HealthIncident
from web.jobs import utc_now
from web.stats import StatsSync, SyncStatus, save_status
from web.status import LEVELS, Item, Problem, ServiceStatus, check_all

logger = logging.getLogger(__name__)

CHECK_INTERVAL = timedelta(minutes=5)
REOPEN_WITHIN = timedelta(minutes=15)
KEEP = timedelta(days=365)
STATUS_SETTING = "health_check"
LABELS = {
    "plex": "Plex",
    "radarr": "Radarr",
    "sonarr": "Sonarr",
    "bazarr": "Bazarr",
    "sabnzbd": "SABnzbd",
    "seerr": "Seerr",
}
# work waiting for someone rather than something wrong, left out of the history
NOT_RECORDED = {("seerr", "pending"), ("seerr", "issues")}
# radarr/sonarr cards list stuck queue records as items, plex lists streams
QUEUE_SERVICES = ("radarr", "sonarr")


@dataclass(frozen=True)
class Issue:
    key: str
    level: str  # warn or bad
    text: str


def problem_key(problem: Problem) -> str:
    return (problem.key or problem.text)[:500]


def item_key(item: Item) -> str:
    """a stuck queue record's key"""
    return f"queue:{item.title}"[:500]


def issues(status: ServiceStatus) -> list[Issue]:
    """what's wrong with a service now, as the history records it"""
    if status.error:
        return [Issue("down", "bad", f"{status.label} can't be checked: {status.error}")]
    found = [
        Issue(problem_key(p), p.level, p.text[:1000])
        for p in status.problems
        if p.level in ("warn", "bad") and (status.service, p.key) not in NOT_RECORDED
    ]
    if status.service in QUEUE_SERVICES:
        found += [
            Issue(item_key(item), item.level, f"Stuck in the queue: {item.title}: {item.detail}"[:1000])
            for item in status.items
            if item.level in ("warn", "bad")
        ]
    # one incident per key, the worst of them
    by_key: dict[str, Issue] = {}
    for issue in found:
        if issue.key not in by_key or LEVELS[issue.level] > LEVELS[by_key[issue.key].level]:
            by_key[issue.key] = issue
    return list(by_key.values())


def _worse(a: str, b: str) -> str:
    return a if LEVELS.get(a, 0) >= LEVELS.get(b, 0) else b


def record(engine: Engine, statuses: Iterable[ServiceStatus], now: datetime) -> int:
    """open, update and close incidents from one round of checks, delete those that
    ended more than KEEP ago. Returns the incidents open now"""
    with Session(engine) as session:
        session.execute(delete(HealthIncident).where(HealthIncident.ended_at < now - KEEP))
        current = {
            (i.service, i.key): i
            for i in session.scalars(select(HealthIncident).where(HealthIncident.ended_at.is_(None)))
        }
        # newest last, so the latest wins
        recent = {
            (i.service, i.key): i
            for i in session.scalars(
                select(HealthIncident)
                .where(HealthIncident.ended_at >= now - REOPEN_WITHIN)
                .order_by(HealthIncident.ended_at)
            )
        }
        for status in statuses:
            seen = set()
            for issue in issues(status):
                key = (status.service, issue.key)
                seen.add(key)
                incident = current.get(key) or recent.get(key)
                if incident is None:
                    incident = HealthIncident(
                        service=status.service, key=issue.key, level=issue.level,
                        text=issue.text, started_at=now, last_seen_at=now,
                    )
                    session.add(incident)
                    current[key] = incident
                    logger.info("HEALTH\t %s: %s", status.label, issue.text)
                else:
                    incident.level = _worse(incident.level, issue.level)
                    incident.text = issue.text
                    incident.last_seen_at = now
                    incident.ended_at = None
                    current[key] = incident
            for key, incident in current.items():
                if key[0] != status.service or key in seen or incident.ended_at is not None:
                    continue
                # a service that can't be checked only clears its "down"
                if status.error and key[1] != "down":
                    continue
                incident.ended_at = now
                logger.info("HEALTH\t %s cleared: %s", status.label, incident.text)
        session.commit()
        return sum(i.ended_at is None for i in current.values())


def check(engine: Engine, settings: ConfigSettings) -> SyncStatus:
    """one round of checks, recorded. Never raises: the error is in the status"""
    status = SyncStatus(started_at=utc_now())
    try:
        record(engine, check_all(settings, status.started_at), status.started_at)  # type: ignore[arg-type]
    except Exception as e:
        # check_all keeps each service's errors (redacted) on its card, this is the db
        status.error = f"{type(e).__name__}: {e}"
        logger.error("HEALTH\t Check failed: %s", status.error)
    status.finished_at = utc_now()
    save_status(engine, status, STATUS_SETTING)
    return status


class HealthMonitor(StatsSync):
    """checks at start, every CHECK_INTERVAL, and when sync_now() is called"""

    interval = CHECK_INTERVAL
    status_key = STATUS_SETTING
    thread_name = "health-check"

    def sync(self, settings: ConfigSettings) -> SyncStatus:
        return check(self.engine, settings)


# ---------------------------------------------------------------------------
# what the pages show
# ---------------------------------------------------------------------------


def incidents(engine: Engine, since: datetime, service: str | None = None) -> list[HealthIncident]:
    """incidents that lasted into the period (still open, or ended since then), newest
    first"""
    query = select(HealthIncident).where(
        (HealthIncident.ended_at.is_(None)) | (HealthIncident.ended_at >= since)
    )
    if service:
        query = query.where(HealthIncident.service == service)
    with Session(engine) as session:
        return list(session.scalars(query.order_by(HealthIncident.started_at.desc(), HealthIncident.id.desc())))


def lasted(incident: HealthIncident, now: datetime) -> timedelta:
    return (incident.ended_at or now) - incident.started_at


def span(delta: timedelta) -> str:
    """2d 4h, 3h 12m, 25m; under a check apart is "under 5m" """
    minutes = int(delta.total_seconds() // 60)
    if minutes < CHECK_INTERVAL.total_seconds() // 60:
        return f"under {int(CHECK_INTERVAL.total_seconds() // 60)}m"
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def incident_dict(incident: HealthIncident, now: datetime) -> dict[str, Any]:
    return {
        "id": incident.id,
        "service": incident.service,
        "label": LABELS.get(incident.service, incident.service),
        "key": incident.key,
        "level": incident.level,
        "text": incident.text,
        "started_at": incident.started_at,
        "last_seen_at": incident.last_seen_at,
        "ended_at": incident.ended_at,
        "ongoing": incident.ended_at is None,
        "seconds": int(lasted(incident, now).total_seconds()),
        "lasted": span(lasted(incident, now)),
    }


def summary(rows: list[HealthIncident], since: datetime, now: datetime) -> list[dict[str, Any]]:
    """per service: incidents, how many are open, and how long it was down within the
    period"""
    by_service: dict[str, list[HealthIncident]] = defaultdict(list)
    for incident in rows:
        by_service[incident.service].append(incident)
    result = []
    for service in LABELS:
        group = by_service.get(service, [])
        if not group:
            continue
        down = sum(
            ((i.ended_at or now) - max(i.started_at, since) for i in group if i.key == "down"),
            timedelta(),
        )
        result.append(
            {
                "service": service,
                "label": LABELS[service],
                "incidents": len(group),
                "ongoing": sum(i.ended_at is None for i in group),
                "down_seconds": int(down.total_seconds()),
                "down": span(down) if down else None,
            }
        )
    return result
