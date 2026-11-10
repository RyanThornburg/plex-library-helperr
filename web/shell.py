"""shell.py - the app shell around every page: sidebar sections, where you are, and the
badges and header pill that say what needs you from any page.

Everything here comes from the database (open health incidents, removers plans, failed
runs), never from the services, so it's cheap enough for every page and works while a
service is down. A failing query leaves the shell quiet rather than breaking the page."""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from web.db import HealthIncident, JobRun
from web.health import LABELS
from web.jobs import utc_now

logger = logging.getLogger(__name__)

# a failed run is news for a day, after that it's history on Runs
FAILED_WITHIN = timedelta(hours=24)

# the stuck queues the Status section links to, while they have stuck downloads
QUEUES = ("radarr", "sonarr")


@dataclass
class Badge:
    count: int
    # info (work to do), warn or bad
    level: str
    # says what the count is, for screen readers and the tooltip
    label: str


@dataclass
class NavItem:
    key: str
    label: str
    href: str
    icon: str = ""
    current: bool = False
    badge: Badge | None = None
    children: list["NavItem"] = field(default_factory=list)
    # the section holding the current page: it shows its sub pages
    open: bool = False


@dataclass
class Shell:
    sections: list[NavItem]
    # "Sonarr needs you", linking to Status; None when nothing does
    pill: str | None = None
    pill_level: str = "warn"


# (key, label, href, icon, sub pages as (key, label, href, the paths they cover))
SECTIONS = [
    ("status", "Status", "/status", "status", [
        ("history", "Incident history", "/status/history", ("/status/history",)),
    ]),
    ("activity", "Activity", "/runs", "activity", [
        ("runs", "Runs", "/runs", ("/runs", "/jobs/")),
        ("schedules", "Schedules", "/schedules", ("/schedules",)),
        ("events", "Events", "/events", ("/events",)),
        ("logs", "Log files", "/logs", ("/logs",)),
    ]),
    ("removers", "Removers", "/removers", "trash", []),
    ("requests", "Requests", "/requests", "inbox", []),
    ("playback", "Playback", "/playback", "playback", []),
    ("settings", "Settings", "/settings", "settings", [
        ("general", "General", "/settings", ("/settings",)),
        ("connections", "Connections", "/settings/connections", ("/settings/connections",)),
        ("radarr", "Radarr", "/settings/radarr", ("/settings/radarr",)),
        ("sonarr", "Sonarr", "/settings/sonarr", ("/settings/sonarr",)),
        ("trash", "Empty trash", "/settings/trash", ("/settings/trash",)),
        ("webhook", "Webhook", "/settings/webhook", ("/settings/webhook", "/settings/api-key")),
        ("history", "History", "/settings/history", ("/settings/history",)),
    ]),
]


def covers(path: str, prefix: str) -> bool:
    """path is prefix or a page under it ("/runs" covers "/runs/7", not "/runsx")"""
    if prefix.endswith("/"):
        return path.startswith(prefix)
    return path == prefix or path.startswith(prefix + "/")


def section_of(path: str) -> str | None:
    """the sidebar section a page belongs to"""
    # remover rules are edited from Removers, though their forms live under Settings
    if path.startswith("/removers") or ("/removers/" in path and path.startswith("/settings/")):
        return "removers"
    if path.startswith("/settings"):
        return "settings"
    if covers(path, "/status"):
        return "status"
    if any(covers(path, p) for p in ("/runs", "/schedules", "/events", "/logs")) or path.startswith("/jobs/"):
        return "activity"
    for key in ("requests", "playback"):
        if covers(path, f"/{key}"):
            return key
    return None


def sub_of(path: str, subs: list[tuple[str, str, str, tuple[str, ...]]]) -> str | None:
    """the sub page a path belongs to, the longest match winning (/settings/radarr over
    /settings)"""
    best, length = None, -1
    for key, _label, _href, prefixes in subs:
        for prefix in prefixes:
            if covers(path, prefix) and len(prefix) > length:
                best, length = key, len(prefix)
    return best


@dataclass
class Needs:
    """what needs you, read from the database"""

    # service -> worst level of its open incidents
    services: dict[str, str] = field(default_factory=dict)
    # services with stuck downloads in their queue
    stuck: set[str] = field(default_factory=set)
    plans: int = 0
    failed: int = 0


def read_needs(request: Request) -> Needs:
    needs = Needs()
    state = request.app.state
    try:
        with Session(state.engine) as session:
            rows = session.execute(
                select(HealthIncident.service, HealthIncident.level, HealthIncident.key).where(
                    HealthIncident.ended_at.is_(None)
                )
            )
            for service, level, key in rows:
                if needs.services.get(service) != "bad":
                    needs.services[service] = level
                if key.startswith("queue:"):
                    needs.stuck.add(service)
            needs.failed = session.scalar(
                select(func.count(JobRun.id)).where(
                    JobRun.status == "failed", JobRun.finished_at >= utc_now() - FAILED_WITHIN
                )
            ) or 0
            # the newest plan is the one to review: an older one is superseded
            plan = session.scalars(
                select(JobRun)
                .where(JobRun.job == "removers", JobRun.plan_run_id.is_(None), JobRun.status == "succeeded")
                .order_by(JobRun.id.desc())
                .limit(1)
            ).first()
            runner = getattr(state, "runner", None)
            if plan is not None and runner is not None and runner.apply_problem(session, plan) is None:
                needs.plans = 1
    except SQLAlchemyError, ValueError:
        logger.warning("WEB\t Couldn't read what needs you for the sidebar", exc_info=True)
        return Needs()
    return needs


def plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def build_shell(path: str, needs: Needs) -> Shell:
    section = section_of(path)
    sections = []
    for key, label, href, icon, subs in SECTIONS:
        item = NavItem(key, label, href, icon, open=key == section)
        if key == "status":
            subs = subs + [
                (f"queue-{s}", f"{LABELS[s]} queue", f"/status/{s}/queue", (f"/status/{s}/queue",))
                for s in QUEUES
                if s in needs.stuck or covers(path, f"/status/{s}/queue")
            ]
            if needs.services:
                level = "bad" if "bad" in needs.services.values() else "warn"
                n = len(needs.services)
                item.badge = Badge(n, level, plural(n, "service needs you", "services need you"))
        if key == "activity" and (needs.plans or needs.failed):
            parts = []
            if needs.plans:
                parts.append(plural(needs.plans, "plan to review", "plans to review"))
            if needs.failed:
                parts.append(plural(needs.failed, "failed run in the last day", "failed runs in the last day"))
            item.badge = Badge(needs.plans + needs.failed, "bad" if needs.failed else "info", ", ".join(parts))
        if item.open:
            current = sub_of(path, subs)
            item.children = [NavItem(k, lbl, h, current=k == current) for k, lbl, h, _ in subs]
            item.current = current is None
        sections.append(item)

    pill, level = None, "warn"
    if needs.services:
        names = sorted(needs.services, key=lambda s: (needs.services[s] != "bad", s))
        pill = f"{LABELS.get(names[0], names[0])} needs you" if len(names) == 1 else f"{len(names)} services need you"
        level = "bad" if "bad" in needs.services.values() else "warn"
    return Shell(sections, pill, level)


def shell_context(request: Request) -> dict[str, Shell]:
    """a Jinja2Templates context processor: every page's shell"""
    return {"shell": build_shell(request.url.path, read_needs(request))}
