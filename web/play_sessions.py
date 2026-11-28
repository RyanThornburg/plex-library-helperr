"""play_sessions.py - every Plex session Tautulli sees, from its playback start, stop and
error webhooks, to catch someone who can't start a title (or anything on one device) and
gives up without saying so.

Tautulli's history drops a play shorter than its ignore interval (2 minutes by default),
so a title that never starts leaves no trace there, or in web.playback. Its notification
agents still fire for those sessions: a webhook agent posts each to /webhooks/tautulli
(web.webhooks), stored as a PlaySession. A session that stops quickly having barely moved
is a failed start (failed()). One now and then is someone changing their mind; TRIES of
one title since it last played for that person, or DEVICE_TRIES of several titles on one
device, is something to look at: a problem on the Status page's Plex card (problems()),
until the title (or device) plays or ALERT_FOR passes. The Playback page lists them all
(report())"""

import logging
from collections import defaultdict
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from web.db import PlaySession
from web.status import Problem
from web.sync import as_int, as_text

logger = logging.getLogger(__name__)

# tautulli's actions that are stored, by the webhook's "event"
EVENTS = ("play", "stop", "error")
KEEP = timedelta(days=365)
# a stop or error belongs to the open session with its key that started at most this long
# ago; an older one lost its stop
OPEN_FOR = timedelta(days=1)
# a failed start stopped within SHORT of streaming having played under PLAYED (plex
# reports where it is every ten seconds or so, so a few seconds of play can read as none)
SHORT = timedelta(minutes=2)
PLAYED = timedelta(seconds=30)
# failed starts of one title by one person since it last played for them, within ALERT_FOR
TRIES = 3
# or failed starts on one person's device of DEVICE_TITLES titles or more, since anything
# last played there
DEVICE_TRIES = 4
DEVICE_TITLES = 2
ALERT_FOR = timedelta(days=3)
# the Playback page's list
SHOWN = 50

# the webhook's fields: tautulli's notification parameters, in its JSON Data template
TEMPLATE = {
    "event": "{action}",
    "session_key": "{session_key}",
    "user_id": "{user_id}",
    "user": "{user}",
    "rating_key": "{rating_key}",
    "title": "{title}",
    "media_type": "{media_type}",
    "player": "{player}",
    "product": "{product}",
    "platform": "{platform}",
    "location": "{stream_location}",
    "quality_profile": "{quality_profile}",
    "transcode_decision": "{transcode_decision}",
    "video_decision": "{video_decision}",
    "audio_decision": "{audio_decision}",
    "subtitle_decision": "{subtitle_decision}",
    "container": "{container}",
    "video_codec": "{video_codec}",
    "video_resolution": "{video_full_resolution}",
    "audio_codec": "{audio_codec}",
    "audio_channels": "{audio_channels}",
    "audio_profile": "{audio_profile}",
    "offset": "{progress_duration_sec}",
    "stream_seconds": "{stream_duration_sec}",
}
# tautulli's trigger names for EVENTS, as its webhook agent's Triggers tab shows them
TRIGGERS = ("Playback Start", "Playback Stop", "Playback Error")
DECISIONS = {"direct play": "direct play", "copy": "direct stream", "transcode": "transcode"}
CODECS = {"dca": "DTS", "dca-ma": "DTS-HD MA", "truehd": "TrueHD", "mpeg2video": "MPEG-2"}


# ---------------------------------------------------------------------------
# the webhook
# ---------------------------------------------------------------------------


def _fields(payload: dict[str, Any]) -> dict[str, Any]:
    """a PlaySession's stream columns from the webhook, those it sent"""
    text = {
        "user": 255, "title": 500, "media_type": 16, "player": 255, "product": 64, "platform": 64,
        "location": 16, "quality_profile": 64, "transcode_decision": 32, "video_decision": 32,
        "audio_decision": 32, "subtitle_decision": 32, "container": 32, "video_codec": 32,
        "video_resolution": 16, "audio_codec": 32, "audio_profile": 64,
    }
    found: dict[str, Any] = {k: v for k, n in text.items() if (v := as_text(payload.get(k), n)) is not None}
    for key in ("rating_key", "audio_channels"):
        if (number := as_int(payload.get(key))) is not None:
            found[key] = number
    return found


def _open(session: Session, key: str, now: datetime) -> PlaySession | None:
    return session.scalars(
        select(PlaySession)
        .where(PlaySession.session_key == key, PlaySession.ended_at.is_(None), PlaySession.started_at >= now - OPEN_FOR)
        .order_by(PlaySession.started_at.desc(), PlaySession.id.desc())
        .limit(1)
    ).first()


def record(engine: Engine, payload: dict[str, Any], now: datetime) -> tuple[bool, str | None]:
    """stores one webhook: (stored, a line for the log when it was a failed start)"""
    event = payload.get("event")
    key = as_text(payload.get("session_key"), 64)
    user_id = as_int(payload.get("user_id"))
    if event not in EVENTS or key is None or user_id is None:
        return False, None
    offset = as_int(payload.get("offset"))
    seconds = as_int(payload.get("stream_seconds"))
    fields = _fields(payload)
    with Session(engine) as session:
        found = _open(session, key, now)
        if event == "play":
            session.execute(delete(PlaySession).where(PlaySession.started_at < now - KEEP))
            if found is not None and found.rating_key == fields.get("rating_key"):
                # sent twice (a resumed session can start again): it's the same one
                _update(found, fields)
                session.commit()
                return True, None
            if found is not None:
                # its stop never came; how long it streamed isn't known
                found.ended_at = now
            session.add(
                PlaySession(
                    session_key=key, started_at=now, user_id=user_id, user=fields.pop("user", "") or str(user_id),
                    title=fields.pop("title", "") or "?", start_offset=offset, **fields,
                )
            )
            session.commit()
            return True, None
        if found is None:
            # its start wasn't seen (sent before the webhook was set up, or lost)
            found = PlaySession(
                session_key=key, started_at=now - timedelta(seconds=max(seconds or 0, 0)), user_id=user_id,
                user=fields.get("user") or str(user_id), title=fields.get("title") or "?",
            )
            session.add(found)
        _update(found, fields)
        if offset is not None:
            found.end_offset = offset
        if seconds is not None:
            found.stream_seconds = max(seconds, 0)
        if event == "error":
            found.error = True
        else:
            found.ended_at = now
        said = describe_failure(found) if event == "stop" and failed(found) else None
        session.commit()
    return True, said


def _update(row: PlaySession, fields: dict[str, Any]) -> None:
    for name, value in fields.items():
        setattr(row, name, value)


def describe_failure(s: PlaySession) -> str:
    """e.g. majcher33 stopped Primetime on TV 2025 after 27s at 0:00 (direct play)"""
    at = "" if s.end_offset is None else f" at {clock(s.end_offset)}"
    after = "" if s.stream_seconds is None else f" after {s.stream_seconds}s"
    error = ", Plex reported an error" if s.error else ""
    return f"{s.user} stopped {s.title} on {player_name(s)}{after}{at} ({stream(s)}){error}"


def last_received(engine: Engine) -> datetime | None:
    """the newest session's start or end, for Settings → Webhook"""
    with Session(engine) as session:
        return session.scalar(select(func.max(func.coalesce(PlaySession.ended_at, PlaySession.started_at))))



# ---------------------------------------------------------------------------
# what a session was
# ---------------------------------------------------------------------------


def clock(seconds: int) -> str:
    """1:02:03, 0:27"""
    minutes, seconds = divmod(max(seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def player_name(s: PlaySession) -> str:
    return s.player or s.product or s.platform or "an unknown player"


def when(s: PlaySession) -> datetime:
    return s.ended_at or s.started_at


def played(s: PlaySession) -> int | None:
    """seconds of the item it got through, None when that isn't known"""
    if s.end_offset is None:
        return None
    if s.start_offset is None:
        # where it started wasn't seen: from the start of the item
        return s.end_offset
    return s.end_offset - s.start_offset


def failed(s: PlaySession) -> bool:
    """Plex reported an error, or it stopped within SHORT of streaming having played
    under PLAYED"""
    if s.error:
        return True
    if s.ended_at is None or s.stream_seconds is None or s.stream_seconds >= SHORT.total_seconds():
        return False
    got = played(s)
    return got is None or got < PLAYED.total_seconds()


def succeeded(s: PlaySession, now: datetime) -> bool:
    """it played: ended without failing, or still open after SHORT, longer than any failed
    start lasts (tautulli usually sends no stop for a session watched past its watched
    percent)"""
    if s.error:
        return False
    if s.ended_at is None:
        return s.started_at <= now - SHORT
    return s.stream_seconds is not None and not failed(s)


def _codec(codec: str | None) -> str:
    return CODECS.get(codec or "", (codec or "").upper())


def _channels(channels: int | None) -> str:
    return {1: "mono", 2: "stereo", 6: "5.1", 7: "6.1", 8: "7.1"}.get(channels or 0, f"{channels}ch" if channels else "")


def file_format(s: PlaySession) -> str:
    """the file, e.g. H264 1080p, EAC3 Atmos 5.1"""
    video = " ".join(v for v in (_codec(s.video_codec), s.video_resolution) if v)
    atmos = "Atmos" if s.audio_profile and "atmos" in s.audio_profile.casefold() else ""
    audio = " ".join(v for v in (_codec(s.audio_codec), atmos, _channels(s.audio_channels)) if v)
    return ", ".join(v for v in (video, audio) if v)


def stream(s: PlaySession) -> str:
    """how plex sent it and the file, e.g. direct play of H264 1080p, EAC3 Atmos 5.1"""
    decision = DECISIONS.get(s.transcode_decision or "", s.transcode_decision or "")
    if s.quality_profile and s.quality_profile != "Original":
        decision = f"{decision} at {s.quality_profile}" if decision else s.quality_profile
    found = file_format(s)
    if decision and found:
        return f"{decision} of {found}"
    return decision or found or "no stream details"


def span(delta: timedelta) -> str:
    """2d 4h, 3h 12m, 25m, under a minute"""
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


# ---------------------------------------------------------------------------
# what to look at
# ---------------------------------------------------------------------------


@dataclass
class Trouble:
    """failed starts worth a look: of one title by one person, or of several titles on one
    person's device. Sessions oldest first"""

    kind: str  # title, device
    user_id: int
    user: str
    player: str
    failures: list[PlaySession]

    @property
    def last(self) -> PlaySession:
        return self.failures[-1]

    @property
    def titles(self) -> list[str]:
        return list(dict.fromkeys(s.title for s in self.failures))

    @property
    def key(self) -> str:
        """the Plex card's problem key, so the incident lasts while the count goes up"""
        if self.kind == "device":
            return f"start:{self.user_id}:device:{self.player}"[:500]
        return f"start:{self.user_id}:{self.last.rating_key or self.last.title}"[:500]

    def text(self, now: datetime) -> str:
        tries = len(self.failures)
        over = span(when(self.last) - self.failures[0].started_at)
        ago = span(now - when(self.last))
        if self.kind == "device":
            return (
                f"{self.user} couldn't start {len(self.titles)} titles on {self.player}: {tries} tries in {over}, "
                f"the last {ago} ago ({self.last.title}, {stream(self.last)})"
            )
        return (
            f"{self.user} couldn't start {self.last.title} on {self.player}: {tries} tries in {over}, "
            f"the last {ago} ago ({stream(self.last)})"
        )

    def to_dict(self, now: datetime) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "user_id": self.user_id,
            "user": self.user,
            "player": self.player,
            "titles": self.titles,
            "tries": len(self.failures),
            "first_at": self.failures[0].started_at,
            "last_at": when(self.last),
            "stream": stream(self.last),
            "remote": self.last.location == "wan",
            "text": self.text(now),
        }


def _since_success(group: Sequence[PlaySession], now: datetime) -> list[PlaySession]:
    """the failed starts after the group's last session that played, oldest first"""
    ordered = sorted(group, key=lambda s: (when(s), s.id or 0))
    ok = [s for s in ordered if succeeded(s, now)]
    last_ok = when(ok[-1]) if ok else None
    return [s for s in ordered if failed(s) and (last_ok is None or when(s) > last_ok)]


def troubles(sessions: Iterable[PlaySession], now: datetime) -> list[Trouble]:
    """failed starts worth a look now, newest first: a device several titles failed on,
    then titles that failed TRIES times for one person (those on a device already listed
    are part of it). Within ALERT_FOR, after the last session of it that played"""
    recent = [s for s in sessions if when(s) >= now - ALERT_FOR]
    by_device: dict[tuple[int, str], list[PlaySession]] = defaultdict(list)
    by_title: dict[tuple[int, Hashable], list[PlaySession]] = defaultdict(list)
    for s in recent:
        by_device[s.user_id, player_name(s)].append(s)
        by_title[s.user_id, s.rating_key if s.rating_key is not None else s.title].append(s)
    found = []
    devices = set()
    for (user_id, player), group in by_device.items():
        failures = _since_success(group, now)
        if len(failures) >= DEVICE_TRIES and len({s.rating_key or s.title for s in failures}) >= DEVICE_TITLES:
            found.append(Trouble("device", user_id, failures[-1].user, player, failures))
            devices.add((user_id, player))
    for (user_id, _), group in by_title.items():
        failures = _since_success(group, now)
        if len(failures) < TRIES:
            continue
        player = player_name(failures[-1])
        if (user_id, player) not in devices:
            found.append(Trouble("title", user_id, failures[-1].user, player, failures))
    found.sort(key=lambda t: when(t.last), reverse=True)
    return found


def recent_sessions(engine: Engine, now: datetime, user_id: int | None = None) -> list[PlaySession]:
    """what troubles() needs: sessions that started within ALERT_FOR, and a little before
    (one that started then and ended within it)"""
    return window_sessions(engine, now - ALERT_FOR - OPEN_FOR, user_id)


def problems(engine: Engine, now: datetime) -> list[Problem]:
    """the Plex card's problems: each trouble a warning"""
    return [Problem("warn", t.text(now), t.key) for t in troubles(recent_sessions(engine, now), now)]


def window_sessions(engine: Engine, since: datetime, user_id: int | None = None) -> list[PlaySession]:
    """sessions that started since then, newest first"""
    query = select(PlaySession).where(PlaySession.started_at >= since)
    if user_id is not None:
        query = query.where(PlaySession.user_id == user_id)
    with Session(engine) as session:
        return list(session.scalars(query.order_by(PlaySession.started_at.desc(), PlaySession.id.desc())))


def report(sessions: Sequence[PlaySession], recent: Sequence[PlaySession], now: datetime) -> dict[str, Any]:
    """the Playback page's part: troubles now (from `recent`, recent_sessions()), and the
    failed starts in the window (`sessions`, newest first) grouped by person, title and
    device, newest first, each saying whether it played for them since"""
    by_title: dict[tuple[int, Hashable], list[PlaySession]] = defaultdict(list)
    for s in sessions:
        by_title[s.user_id, s.rating_key if s.rating_key is not None else s.title].append(s)
    rows = []
    groups: dict[tuple[int, Hashable, str], list[PlaySession]] = defaultdict(list)
    for s in sessions:
        if failed(s):
            groups[s.user_id, s.rating_key if s.rating_key is not None else s.title, player_name(s)].append(s)
    for (user_id, item, player), group in groups.items():
        last = group[0]
        later = [o for o in by_title[user_id, item] if succeeded(o, now) and when(o) > when(last)]
        rows.append(
            {
                "user_id": user_id,
                "user": last.user,
                "title": last.title,
                "player": player,
                "platform": last.platform,
                "remote": last.location == "wan",
                "tries": len(group),
                "errors": sum(s.error for s in group),
                "last_at": when(last),
                "stream": stream(last),
                "played_since": when(later[-1]) if later else None,
            }
        )
    rows.sort(key=lambda r: r["last_at"], reverse=True)
    failures = sum(r["tries"] for r in rows)
    return {
        "sessions": len(sessions),
        "failed": failures,
        "troubles": [t.to_dict(now) for t in troubles(recent, now)],
        "rows": rows[:SHOWN],
        "more": max(len(rows) - SHOWN, 0),
    }
