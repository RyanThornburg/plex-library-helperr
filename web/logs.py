"""logs.py - the app's log as events, like Sonarr's System -> Events, and its log files.

The web app writes logs/web.log (logging_config, rotated at 500 KB, two old files kept),
so everything outside a job run (webhooks, syncs, health checks, startup, errors) can be
read here instead of in the container. Secrets are left out when lines are written
(SecretFilter); what's read is redacted again in case one was logged before it was known.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import logging_config
from logging_config import redacted

# the web app's log, then its rotated files, newest first
WEB_LOG = "web.log"
# events shown per page
PAGE = 100
# levels the filter shows from, lowest first
LEVELS = {"info": 20, "warning": 30, "error": 40}
LEVEL_NUMBERS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
# a log file a page may list and download: what the handler writes and rotates
FILE_NAME = re.compile(r"^[A-Za-z0-9_-]+\.log(\.\d+)?$")

# logging_config's format: "%(levelname)s %(asctime)s [%(filename)s:%(lineno)d]\t %(message)s"
LINE = re.compile(
    r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL) (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d{3} \[([^\]]*)\]\t ?(.*)$"
)
# most of the app's messages start with what they're about: "HEALTH\t ...", "SONARR\t ..."
COMPONENT = re.compile(r"^([A-Z][A-Z0-9_ -]{1,23})\t\s*(.*)$", re.S)
# uvicorn's access log, one line per http request
REQUEST_SOURCES = ("h11_impl.py", "httptools_impl.py")


def log_dir() -> Path:
    # read when called, so tests can point it at their own dir
    return logging_config.LOG_DIR


@dataclass
class Event:
    level: str  # INFO, WARNING, ERROR...
    # naive utc, like the rest of the app. The file has the container's local time
    time: datetime
    source: str  # file:line
    component: str
    message: str
    # lines after the first: a traceback, or a message that spans lines
    details: list[str] = field(default_factory=list)

    @property
    def request(self) -> bool:
        return self.source.split(":")[0] in REQUEST_SOURCES

    @property
    def number(self) -> int:
        return LEVEL_NUMBERS.get(self.level, 20)

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level.lower(),
            "time": self.time.isoformat(timespec="seconds") + "Z",
            "source": self.source,
            "component": self.component,
            "message": self.message,
            "details": "\n".join(self.details),
        }


def local_to_utc(text: str) -> datetime:
    """the log's time is the container's local time (asctime): naive utc for the pages"""
    local = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def parse(text: str) -> list[Event]:
    """a log file's events, oldest first. Lines that don't start an event (traceback
    lines) belong to the one before; any before the first event are dropped"""
    events: list[Event] = []
    for line in text.splitlines():
        match = LINE.match(line)
        if match is None:
            if events:
                events[-1].details.append(line)
            continue
        level, time, source, message = match.groups()
        component = ""
        if found := COMPONENT.match(message):
            component, message = found.group(1).strip(), found.group(2)
        events.append(Event(level, local_to_utc(time), source, component, message))
    return events


def web_log_files(directory: Path) -> list[Path]:
    """web.log and its rotated files, oldest first"""
    files = [directory / f"{WEB_LOG}.{n}" for n in (2, 1)] + [directory / WEB_LOG]
    return [f for f in files if f.is_file()]


def read_events(directory: Path | None = None) -> list[Event]:
    """every event in the web app's log files, newest first"""
    events: list[Event] = []
    for path in web_log_files(directory or log_dir()):
        events += parse(redacted(path.read_text(errors="replace")))
    events.reverse()
    return events


@dataclass
class EventFilters:
    level: str = "info"
    # http requests (uvicorn's access log) are left out unless asked for
    requests: bool = False
    page: int = 1

    def keep(self, event: Event) -> bool:
        return event.number >= LEVELS[self.level] and (self.requests or not event.request)

    def query(self, **changes: object) -> dict[str, object]:
        """the page's query string with some values changed, defaults left out"""
        values = {"level": self.level, "requests": self.requests, "page": self.page} | changes
        return {
            k: (1 if v is True else v)
            for k, v in values.items()
            if v not in (None, False) and not (k == "level" and v == "info") and not (k == "page" and v == 1)
        }


@dataclass
class EventPage:
    events: list[Event]
    total: int  # events the filter keeps
    pages: int
    filters: EventFilters
    # the oldest event in the files, how far back the log goes
    oldest: datetime | None


def event_page(filters: EventFilters, directory: Path | None = None) -> EventPage:
    events = read_events(directory)
    kept = [e for e in events if filters.keep(e)]
    pages = max(1, -(-len(kept) // PAGE))
    page = min(filters.page, pages)
    shown = kept[(page - 1) * PAGE : page * PAGE]
    return EventPage(shown, len(kept), pages, EventFilters(filters.level, filters.requests, page), events[-1].time if events else None)


@dataclass
class LogFile:
    name: str
    size: int
    modified: datetime  # naive utc


def log_files(directory: Path | None = None) -> list[LogFile]:
    """the log files there, the web app's and the cli's, newest first"""
    found = []
    for path in (directory or log_dir()).iterdir():
        if path.is_file() and FILE_NAME.match(path.name):
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc).replace(tzinfo=None)
            found.append(LogFile(path.name, stat.st_size, modified))
    return sorted(found, key=lambda f: f.modified, reverse=True)


def log_file(name: str, directory: Path | None = None) -> str | None:
    """a log file's text, redacted, or None when there's no such log file. The name must
    be one log_files() lists: no paths, nothing but the logs"""
    if not FILE_NAME.match(name):
        return None
    path = (directory or log_dir()) / name
    if not path.is_file():
        return None
    return redacted(path.read_text(errors="replace"))
