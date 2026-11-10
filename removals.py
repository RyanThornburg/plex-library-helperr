"""removals.py - what Sonarr's and Radarr's removers share: the approved-plan shape, how
bad arr data is reported, and what happens to planned removals (review, auto-approve,
max_deletes, delete)"""

import logging
from collections.abc import Callable, Sequence
from typing import Any, Protocol, TypeVar

from results import Status

logger = logging.getLogger(__name__)

# an applied plan: series/movie id -> the file ids approved for removal
Approved = dict[int, set[int]]


class InvalidArrData(Exception):
    """an arr returned missing/invalid data for a series or movie: it's skipped, never
    guessed at"""


def required_int(raw: dict[str, Any], key: str, error: type[InvalidArrData]) -> int:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise error(f"missing/invalid '{key}': {value!r}")
    return value


class _Rule(Protocol):
    key: str
    plex_library: str | None


class Removal(Protocol):
    @property
    def rule(self) -> _Rule: ...


R = TypeVar("R", bound=Removal)


def act(
    log_name: str,
    unit: str,
    removals: Sequence[R],
    *,
    applying: bool,
    auto_approve: frozenset[str],
    max_deletes: int | None,
    files: Callable[[R], int],
    record: Callable[[R, Status, str], None],
    delete: Callable[[R], str | None],
    removed: Callable[[R], None],
) -> None:
    """record each planned removal and delete the approved ones: all of them when
    applying a reviewed plan, else those of auto-approved rules, the rest planned for
    review. max_deletes is all or nothing for the files this run deletes. delete()
    returns why it failed, or None; removed() is told of each deleted one"""
    live: list[R] = []
    review: list[R] = []
    for r in removals:
        (live if applying or r.rule.key in auto_approve else review).append(r)
    if review:
        logger.info("%s\t DRY RUN - planning %s %s for review, not removing", log_name, len(review), unit)
        for r in review:
            record(r, "dry_run", "")
    if not live:
        return
    if not applying:
        logger.info("%s\t Auto-approved: removing %s %s", log_name, len(live), unit)

    total = sum(files(r) for r in live)
    if max_deletes is not None and total > max_deletes:
        logger.error("%s\t %s files exceeds max_deletes (%s), not removing anything", log_name, total, max_deletes)
        for r in live:
            record(r, "blocked", f"run total of {total} files exceeds max_deletes ({max_deletes})")
        return

    for r in live:
        if (failure := delete(r)) is not None:
            record(r, "failed", failure)
            continue
        record(r, "done", "" if applying else "auto-approved")
        removed(r)
