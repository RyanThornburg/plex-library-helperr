"""kometa.py - Kometa's runs for the Status page. Kometa has no api: its config.yml
webhooks post run_start, error, version and run_end events to /webhooks/kometa
(web.webhooks), which are stored as KometaRun rows, and its card is read from them"""

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from logging_config import redacted
from web.db import KometaRun
from web.status import LEVELS, ServiceStatus
from web.sync import as_int, as_text

logger = logging.getLogger(__name__)

# the events that are stored; changes (with base64 posters) and delete aren't used
EVENTS = ("run_start", "run_end", "error", "version")
KEEP = timedelta(days=30)
# a run_end or error belongs to the run that started at most this long ago and hasn't
# ended; an older one was cut short (kometa restarted)
OPEN_FOR = timedelta(days=1)
# how often kometa runs is read from the runs this long before the latest one, once
# they cover at least CADENCE_SPAN (so a daily pattern, like a longer gap at night, is in)
CADENCE_WINDOW = timedelta(days=7)
CADENCE_SPAN = timedelta(days=1)
# until then: kometa's default schedule is once a day
DEFAULT_CADENCE = timedelta(days=1)
# no run has finished for this much longer than the longest usual gap
STALE_FACTOR = 1.5
STALE_SLACK = timedelta(hours=1)
# run_end's counts, as the card says them
COUNTS = (
    "collections_created",
    "collections_modified",
    "collections_deleted",
    "items_added",
    "items_removed",
    "added_to_radarr",
    "added_to_sonarr",
)
MAX_ERRORS = 100
ERROR_LENGTH = 1000
WHERE_LENGTH = 300


def run_seconds(value: object) -> int | None:
    """kometa's run time, str(timedelta) without the fraction: 0:06:20, 1 day, 2:03:04"""
    if not isinstance(value, str):
        return None
    days, text = 0, value.strip()
    if "day" in text:
        head, _, text = text.partition(",")
        days = as_int(head.split()[0]) if head.split() else None
        if days is None:
            return None
    parts = text.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(p) for p in parts)
    except ValueError:
        return None
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def error_where(payload: dict[str, Any]) -> str:
    """the library and collection (or playlist) an error is about, e.g. Movies › Oscars"""
    library = as_text(payload.get("library_name"), WHERE_LENGTH)
    collection = as_text(payload.get("collection"), WHERE_LENGTH)
    playlist = as_text(payload.get("playlist"), WHERE_LENGTH)
    parts = [library, collection] if collection or not playlist else [f"Playlist {playlist}"]
    return " › ".join(p for p in parts if p)


def _open_run(session: Session, now: datetime) -> KometaRun | None:
    return session.scalars(
        select(KometaRun)
        .where(KometaRun.finished_at.is_(None), KometaRun.started_at >= now - OPEN_FOR)
        .order_by(KometaRun.started_at.desc(), KometaRun.id.desc())
        .limit(1)
    ).first()


def record(engine: Engine, payload: dict[str, Any], now: datetime) -> str | None:
    """stores one webhook, returns what it was for the log (None: not an event that's
    stored). Kometa's own text is redacted again, like everything helperr stores"""
    event = payload.get("event")
    if event not in EVENTS:
        return None
    with Session(engine) as session:
        run = _open_run(session, now)
        if event == "run_start":
            session.execute(delete(KometaRun).where(KometaRun.started_at < now - KEEP))
            session.add(KometaRun(started_at=now, errors=[]))
            said = "Kometa run started"
        elif event == "run_end":
            seconds = run_seconds(payload.get("run_time"))
            if run is None:
                # its start wasn't seen (sent before the webhook was set up, or lost)
                run = KometaRun(started_at=now - timedelta(seconds=seconds or 0), errors=[])
                session.add(run)
            run.finished_at = now
            run.run_seconds = seconds
            run.counts = {key: max(as_int(payload.get(key)) or 0, 0) for key in COUNTS}
            said = f"Kometa run finished in {took(seconds)}: {changes(run.counts) or 'nothing changed'}"
        elif event == "error":
            if run is None:
                run = KometaRun(started_at=now, errors=[])
                session.add(run)
            error = {
                "text": redacted(as_text(payload.get("error"), ERROR_LENGTH) or "Unknown error"),
                "where": redacted(error_where(payload)),
                "critical": payload.get("critical") is True,
            }
            # a new list, so the json column sees the change
            if error not in run.errors and len(run.errors) < MAX_ERRORS:
                run.errors = [*run.errors, error]
            said = f"Kometa {'critical ' if error['critical'] else ''}error{' in ' + error['where'] if error['where'] else ''}: {error['text']}"
        else:
            if run is None:
                return None
            run.version = as_text(payload.get("current"), 64)
            run.latest_version = as_text(payload.get("latest"), 64)
            said = f"Kometa {run.latest_version} is out, running {run.version}"
        session.commit()
    return said


def forget(engine: Engine) -> int:
    """deletes every stored run, so the Status page stops showing Kometa"""
    with Session(engine) as session:
        deleted = session.execute(delete(KometaRun)).rowcount
        session.commit()
    return deleted


def runs(engine: Engine, now: datetime) -> list[KometaRun]:
    """the stored runs, newest first"""
    with Session(engine) as session:
        return list(
            session.scalars(
                select(KometaRun)
                .where(KometaRun.started_at >= now - KEEP)
                .order_by(KometaRun.started_at.desc(), KometaRun.id.desc())
            )
        )


def last_received(engine: Engine) -> datetime | None:
    """when the newest stored run started or finished, for Settings → Webhook"""
    with Session(engine) as session:
        run = session.scalars(select(KometaRun).order_by(KometaRun.started_at.desc()).limit(1)).first()
    return None if run is None else max(run.started_at, run.finished_at or run.started_at)


def span(delta: timedelta) -> str:
    """2d 4h, 3h 12m, 25m"""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "under a minute"
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def took(seconds: int | None) -> str:
    if seconds is None:
        return "an unknown time"
    return f"{seconds}s" if seconds < 60 else span(timedelta(seconds=seconds))


def _counted(parts: list[tuple[int, str]]) -> str:
    return ", ".join(f"{n:,} {what}" for n, what in parts if n)


def changes(counts: dict[str, int] | None) -> str:
    """what a run changed, e.g. collections: 2 created, 5 changed; items: 14 added; 3 sent
    to Radarr. Empty when nothing did"""
    if not counts:
        return ""
    c = counts.get
    collections = _counted(
        [(c("collections_created", 0), "created"), (c("collections_modified", 0), "changed"), (c("collections_deleted", 0), "deleted")]
    )
    items = _counted([(c("items_added", 0), "added"), (c("items_removed", 0), "removed")])
    sent = _counted([(c("added_to_radarr", 0), "sent to Radarr"), (c("added_to_sonarr", 0), "sent to Sonarr")])
    parts = [f"collections: {collections}" if collections else "", f"items: {items}" if items else "", sent]
    return "; ".join(p for p in parts if p)


def cadence(found: Sequence[KometaRun]) -> timedelta | None:
    """the longest gap between run starts in the CADENCE_WINDOW before the latest one,
    None until the runs cover CADENCE_SPAN. Newest first, like runs()"""
    if not found:
        return None
    latest = found[0].started_at
    starts = sorted(r.started_at for r in found if r.started_at >= latest - CADENCE_WINDOW)
    if latest - starts[0] < CADENCE_SPAN:
        return None
    return max(b - a for a, b in zip(starts, starts[1:]))


def kometa_answers(status: ServiceStatus, found: Sequence[KometaRun], now: datetime) -> ServiceStatus:
    """the card, from the stored runs (newest first). Off until kometa has sent one"""
    if not found:
        status.state = "off"
        return status
    latest = found[0]
    finished = next((r for r in found if r.finished_at is not None), None)
    usual = cadence(found)
    limit = (usual or DEFAULT_CADENCE) * STALE_FACTOR + STALE_SLACK
    every = f"; it usually starts one at least every {span(usual)}" if usual else ""
    if finished is None:
        if now - found[-1].started_at > limit:
            status.problem(
                "warn", f"No Kometa run has finished since the first one started {span(now - found[-1].started_at)} ago{every}", "stale"
            )
    elif now - finished.finished_at > limit:  # type: ignore[operator]
        status.problem(
            "warn", f"No Kometa run has finished in {span(now - finished.finished_at)}{every}", "stale"  # type: ignore[operator]
        )
    # the last finished run's errors, and the critical ones of a run since (one that
    # stopped kometa never finishes)
    errors = list(finished.errors) if finished is not None else []
    if latest is not finished:
        errors += [e for e in latest.errors if e.get("critical")]
    seen = set()
    for error in errors:
        text = f"{error['where']}: {error['text']}" if error.get("where") else str(error.get("text"))
        if text not in seen:
            seen.add(text)
            status.problem("bad" if error.get("critical") else "warn", text)
    status.problems.sort(key=lambda p: -LEVELS.get(p.level, 0))
    if latest.latest_version and latest.latest_version != latest.version:
        status.version = latest.version
        status.problem("info", f"Kometa {latest.latest_version} is out")
    running = latest.finished_at is None and now - latest.started_at <= limit
    if running:
        status.facts.append(("Running", f"started {span(now - latest.started_at)} ago"))
    if finished is not None:
        ago = f"{span(now - finished.finished_at)} ago, took {took(finished.run_seconds)}"  # type: ignore[operator]
        status.facts.append(("Last run", ago))
        status.facts.append(("Changed", changes(finished.counts) or "nothing"))
        status.summary = f"Last run {ago}{', running now' if running else ''}"
    else:
        status.summary = "Running its first run" if running else "No run has finished yet"
    if usual:
        status.facts.append(("Runs", f"at least every {span(usual)}"))
    return status


def kometa_status(engine: Engine, now: datetime) -> ServiceStatus:
    """Kometa's card; a database problem is its error rather than the page's"""
    status = ServiceStatus("kometa", "Kometa")
    try:
        found = runs(engine, now)
    except Exception:
        logger.exception("WEB\t Couldn't read Kometa's runs")
        status.error = "Couldn't read Kometa's runs from the database (see the web log)"
        status.state = "bad"
        return status
    return kometa_answers(status, found, now)
