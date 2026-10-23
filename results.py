"""results.py - what a job changed, or would have changed in a dry run"""

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

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
class PlannedFile:
    id: int
    path: str


@dataclass
class PlannedRemoval:
    """one series' removal as planned, kept with the run so it can be reviewed and its
    series approved (see remover.run_removers' `approved`)"""

    series_id: int
    title: str
    rule: str
    labels: list[str]
    files: list[PlannedFile]
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
    removals: list[PlannedRemoval] = field(default_factory=list[PlannedRemoval])

    @property
    def failed(self) -> bool:
        return bool(self.errors) or any(c.status == "failed" for c in self.changes)

    def to_dict(self) -> dict[str, Any]:
        """json-safe, for storing with a job run"""
        return {
            "job": self.job,
            "changes": [asdict(c) for c in self.changes],
            "scan_dirs": sorted(self.scan_dirs),
            "plex_libraries": sorted(self.plex_libraries),
            "errors": list(self.errors),
            "removals": [asdict(r) for r in self.removals],
        }

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
