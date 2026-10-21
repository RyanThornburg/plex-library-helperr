"""results.py - what a job changed, or would have changed in a dry run"""

import logging
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

# done: applied, dry_run: planned only, blocked: a safety check stopped it, failed: an api call failed
Status = Literal["done", "dry_run", "blocked", "failed"]

JOBS = ("movers", "removers", "empty_trash")


@dataclass
class Change:
    """one change to a service: a bulk edit, a series removal, a library's trash, a scan"""

    service: str  # RADARR, SONARR, PLEX, AUTOSCAN
    action: str  # move, profile, remove, empty_trash, scan, refresh
    target: str  # root folder, quality profile, series title or plex library
    items: list[str]  # media titles, season/episode labels or folders
    status: Status
    detail: str = ""


@dataclass
class JobResult:
    """everything a job run changed, the folders/libraries left to scan, and errors that
    stopped part of the job early (details of skipped media stay in the log)"""

    job: str
    changes: list[Change] = field(default_factory=list[Change])
    scan_dirs: set[str] = field(default_factory=set[str])
    plex_libraries: set[str] = field(default_factory=set[str])
    errors: list[str] = field(default_factory=list[str])

    def log_summary(self) -> None:
        logger.info(
            "SUMMARY %s: %s changes, %s errors",
            self.job,
            len(self.changes),
            len(self.errors),
        )
        for c in self.changes:
            extra = [f"{len(c.items)} items"] if c.items else []
            extra += [c.detail] if c.detail else []
            logger.info(
                "SUMMARY %s %s [%s] %s%s",
                c.service,
                c.action,
                c.status,
                c.target,
                f" ({', '.join(extra)})" if extra else "",
            )
        for e in self.errors:
            logger.error("SUMMARY %s", e)
