"""maintenance.py - a maintenance window: something on the server stops containers on
purpose, such as Unraid's Appdata Backup plugin, which stops each container while it
copies its appdata. Its pre-run and post-run scripts run

    docker exec plex-library-helperr python main.py --maintenance start "Appdata Backup"
    docker exec plex-library-helperr python main.py --maintenance end "Appdata Backup"

While a window is open, and for SETTLE after it ends (containers take a minute to answer
again), the health check opens no new incident for what a stopped container looks like
(quieted()): a service that can't be reached, a container that autostarts not running,
Bazarr losing an arr's live updates. Problems already open carry on, and anything still
wrong after the window is recorded then. A window never ended closes after MAX_LENGTH,
so a post-run script that never ran can't silence the health check for good"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from results import utc_now
from web.db import AppSetting

logger = logging.getLogger(__name__)

SETTING = "maintenance"
SETTLE = timedelta(minutes=10)
MAX_LENGTH = timedelta(hours=6)
MAX_SOURCE = 100


@dataclass
class Window:
    source: str
    started_at: datetime
    ended_at: datetime | None = None

    def until(self) -> datetime:
        """when the health check records everything again"""
        return self.ended_at + SETTLE if self.ended_at else self.started_at + MAX_LENGTH

    def active(self, now: datetime) -> bool:
        return self.started_at <= now <= self.until()

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "until": self.until(),
        }


def quieted(service: str, key: str) -> bool:
    """an incident a stopped container would open"""
    return (
        key == "down"
        or (service == "unraid" and key.startswith("container:"))
        or (service == "bazarr" and key.startswith("signalr:"))
    )


def read(session: Session) -> Window | None:
    setting = session.get(AppSetting, SETTING)
    if setting is None:
        return None
    try:
        data = json.loads(setting.value)
        ended = data.get("ended_at")
        return Window(
            str(data["source"]),
            datetime.fromisoformat(data["started_at"]),
            datetime.fromisoformat(ended) if ended else None,
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def active(session: Session, now: datetime) -> Window | None:
    window = read(session)
    return window if window is not None and window.active(now) else None


def current(engine: Engine, now: datetime) -> Window | None:
    with Session(engine) as session:
        return active(session, now)


def _save(session: Session, window: Window, now: datetime) -> None:
    setting = session.get(AppSetting, SETTING) or AppSetting(key=SETTING)
    setting.value = json.dumps({
        "source": window.source,
        "started_at": window.started_at.isoformat(),
        "ended_at": window.ended_at.isoformat() if window.ended_at else None,
    })
    setting.updated_at = now
    session.add(setting)
    session.commit()


def start(engine: Engine, source: str, now: datetime | None = None) -> Window:
    now = now or utc_now()
    window = Window(source.strip()[:MAX_SOURCE] or "Maintenance", now)
    with Session(engine) as session:
        _save(session, window, now)
    return window


def end(engine: Engine, now: datetime | None = None) -> Window | None:
    """ends the open window, None when there's none"""
    now = now or utc_now()
    with Session(engine) as session:
        window = read(session)
        if window is None or window.ended_at is not None or not window.active(now):
            return None
        window.ended_at = now
        _save(session, window, now)
    return window
