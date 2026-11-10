"""sync.py - what the background syncs share (requests: web.stats, playback, health
checks): their thread, the status each saves for its page, and reading the values
Tautulli and Seerr send"""

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from config.models import ConfigSettings
from logging_config import redacted
from web.db import AppSetting
from web.jobs import utc_now

logger = logging.getLogger(__name__)


class SyncError(Exception):
    """a sync that can't run (a connection missing): its message is shown on its page"""


def as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


def as_text(value: object, length: int) -> str | None:
    return str(value)[:length] if value not in (None, "") else None


def _time(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) and value else None
    except ValueError:
        return None


@dataclass
class SyncStatus:
    """the last sync, kept in app_settings so a restart shows it"""

    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    plays_read: int = 0
    requests: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
                "error": self.error,
                "plays_read": self.plays_read,
                "requests": self.requests,
            }
        )


def sync_status(engine: Engine, key: str) -> SyncStatus:
    with Session(engine) as session:
        setting = session.get(AppSetting, key)
    if setting is None:
        return SyncStatus()
    try:
        data = json.loads(setting.value)
        return SyncStatus(
            started_at=_time(data.get("started_at")),
            finished_at=_time(data.get("finished_at")),
            error=data.get("error"),
            plays_read=data.get("plays_read") or 0,
            requests=data.get("requests") or 0,
        )
    except ValueError, AttributeError:
        return SyncStatus()


def save_status(engine: Engine, status: SyncStatus, key: str) -> None:
    """stores a sync's status, its error redacted: it's shown on pages and api answers"""
    if status.error:
        status.error = redacted(status.error)
    with Session(engine) as session:
        setting = session.get(AppSetting, key) or AppSetting(key=key)
        setting.value = status.to_json()
        setting.updated_at = utc_now()
        session.add(setting)
        session.commit()


class BackgroundSync:
    """a sync on its own thread, beside the JobRunner: at start, every interval and when
    sync_now() is called. Subclasses set interval, status_key, thread_name and sync()"""

    interval: timedelta
    status_key: str
    thread_name: str

    def __init__(self, engine: Engine, load_config: Callable[[], ConfigSettings]):
        self.engine = engine
        self.load_config = load_config
        self.running = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=self.thread_name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout)

    def sync_now(self) -> None:
        self._wake.set()

    def run_once(self) -> SyncStatus:
        self.running = True
        try:
            try:
                settings = self.load_config()
            except Exception as e:
                now = utc_now()
                status = SyncStatus(now, now, f"The settings can't be loaded: {e}")
                save_status(self.engine, status, self.status_key)
                return status
            return self.sync(settings)
        finally:
            self.running = False

    def sync(self, settings: ConfigSettings) -> SyncStatus:
        raise NotImplementedError

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:
                # anything a sync doesn't handle (a locked database while saving its
                # status) is logged and the thread goes on, rather than dying and leaving
                # pages showing the last good sync as current
                logger.exception("WEB\t %s failed", self.thread_name)
                try:
                    now = utc_now()
                    save_status(self.engine, SyncStatus(now, now, f"{type(e).__name__}: {e}"), self.status_key)
                except Exception:
                    logger.exception("WEB\t %s couldn't save its status", self.thread_name)
            self._wake.wait(self.interval.total_seconds())
            self._wake.clear()
