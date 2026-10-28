"""playback.py - playback quality: how plex streamed the last few weeks of plays, and why
it transcoded.

PlaybackSync reads Tautulli's new plays every INTERVAL and adds each movie/episode play
with how plex streamed it (get_stream_data) to `playbacks`, kept for KEEP. It's about
where problems happen now, so it starts BACKFILL before its first sync and never reads
older history. report() is what the page and the api show for a window: decisions, why
plays transcoded, by user, by player, the file formats that cause transcodes and how
often, files that transcode for several people, recent transcodes"""

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

import requests
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from config.models import ConfigSettings, TautulliConfig
from watch_history import history_rows
from web.db import Playback
from web.jobs import utc_now
from web.lookups import redact
from web.stats import (
    PLAYS_OVERLAP,
    TAUTULLI_PAGE,
    StatsError,
    StatsSync,
    SyncStatus,
    _int,
    _tautulli,
    _text,
    _utc,
    save_status,
)

logger = logging.getLogger(__name__)

KEEP = timedelta(days=365)
# what the first sync reads, so the page isn't empty on day one
BACKFILL = timedelta(days=30)
INTERVAL = timedelta(minutes=10)
STATUS_SETTING = "playback_sync"
MEDIA_TYPES = {"movie", "episode"}
# plays added per commit, so the first sync's newest plays show while it reads the rest
COMMIT_EVERY = 100

DECISIONS = {"direct play": "Direct play", "copy": "Direct stream", "transcode": "Transcode"}
# why a play transcoded, in the order causes() checks them: (label, what to do)
CAUSES = {
    "quality": (
        "Quality setting",
        "The player's quality is set below Original (or the server's remote stream limit "
        "caps it), so Plex converts the video to fit. Set the player's remote and home "
        "quality to Original or Maximum; if many remote players stop at the same "
        "bitrate, check Plex's Remote Access → Limit remote stream bitrate.",
    ),
    "subtitles": (
        "Subtitles burned in",
        "Image subtitles (PGS, VobSub) or styled ASS can't be sent alongside the video on "
        "this player, so Plex draws them into it. SRT subtitles for the file avoid it.",
    ),
    "hdr": (
        "HDR to SDR",
        "HDR video tone mapped for a player without HDR, the heaviest kind of transcode. "
        "An SDR version, or an HDR capable player, avoids it.",
    ),
    "video_codec": (
        "Video format",
        "The player can't decode the file's video (often HEVC, VC-1 or MPEG-2). An H.264 "
        "version of the file, or a newer player, avoids it.",
    ),
    "resolution": (
        "Resolution",
        "Sent at a lower resolution at Original quality, usually a player that can't "
        "show 4K.",
    ),
    "audio": (
        "Audio format",
        "Only the audio is converted: the player can't play the file's audio (often "
        "DTS, TrueHD or EAC3). Light on the server; an AC3/AAC track or audio "
        "passthrough to a receiver avoids it.",
    ),
    "unknown": (
        "Not recorded",
        "Tautulli doesn't say why. Often the player's automatic quality lowering it on "
        "a slow connection, or a player that can't handle the file's bitrate.",
    ),
}
# causes that follow the file to every player that can't play it, worth fixing in the file
FILE_CAUSES = ("subtitles", "hdr", "video_codec", "audio")
WINDOWS = {
    "24h": ("Last 24 hours", timedelta(hours=24)),
    "7d": ("Last 7 days", timedelta(days=7)),
    "30d": ("Last 30 days", timedelta(days=30)),
    "90d": ("Last 90 days", timedelta(days=90)),
    "180d": ("Last 180 days", timedelta(days=180)),
    "365d": ("Last 12 months", KEEP),
}
DEFAULT_WINDOW = "7d"
RECENT = 50
FILES = 25
FORMATS = 25
# what to change for a person's device that transcodes, by its main cause
CHANGES = {
    "quality": "Original or Maximum",
    "subtitles": "SRT subtitles",
    "hdr": "an SDR version, or an HDR player",
    "video_codec": "an H.264 version, or a newer player",
    "resolution": "a 1080p version",
    "audio": "audio passthrough, or an AC3/AAC track",
    "unknown": "check their connection",
}
# the fix list stops at FIXES devices, once it covers FIXES_COVER of the transcodes, or at
# a device with fewer than FIX_MIN_TRANSCODES (a few are noise, not a setting to change)
FIXES = 8
FIXES_COVER = 0.75
FIX_MIN_TRANSCODES = 3
# a file one person's player can't play is about the player, see the formats table
FILE_MIN_USERS = 2


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def _flag(value: object) -> bool | None:
    number = _int(value)
    return None if number is None else bool(number)


def playback_from(row: Mapping[str, Any], stream: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """a Playback's columns from a get_history row and its get_stream_data (None when
    tautulli has none), None for a play in progress (no id yet), music, or a row it
    can't read"""
    history_id = _int(row.get("id"))
    started_at = _utc(row.get("started") or row.get("date"))
    user_id = _int(row.get("user_id"))
    if history_id is None or started_at is None or user_id is None or row.get("media_type") not in MEDIA_TYPES:
        return None
    s = stream or {}
    return {
        "id": history_id,
        "started_at": started_at,
        "user_id": user_id,
        "user": str(row.get("friendly_name") or row.get("user") or "")[:255],
        "media_type": row["media_type"],
        "rating_key": _int(row.get("rating_key")),
        "title": str(row.get("full_title") or row.get("title") or "")[:500],
        "platform": _text(row.get("platform"), 64),
        "product": _text(row.get("product"), 64),
        "player": _text(row.get("player"), 255),
        "location": _text(row.get("location"), 16),
        "transcode_decision": _text(row.get("transcode_decision"), 32),
        "details": stream is not None,
        "quality_profile": _text(s.get("quality_profile"), 64),
        "container": _text(s.get("container"), 32),
        "stream_container": _text(s.get("stream_container"), 32),
        "container_decision": _text(s.get("stream_container_decision"), 32),
        "video_decision": _text(s.get("video_decision") or s.get("stream_video_decision"), 32),
        "video_codec": _text(s.get("video_codec"), 32),
        "stream_video_codec": _text(s.get("stream_video_codec"), 32),
        "video_resolution": _text(s.get("video_full_resolution"), 16),
        "stream_video_resolution": _text(s.get("stream_video_full_resolution"), 16),
        "video_dynamic_range": _text(s.get("video_dynamic_range"), 32),
        "stream_video_dynamic_range": _text(s.get("stream_video_dynamic_range"), 32),
        "audio_decision": _text(s.get("audio_decision") or s.get("stream_audio_decision"), 32),
        "audio_codec": _text(s.get("audio_codec"), 32),
        "stream_audio_codec": _text(s.get("stream_audio_codec"), 32),
        "audio_channels": _int(s.get("audio_channels")),
        "stream_audio_channels": _int(s.get("stream_audio_channels")),
        "subtitle_decision": _text(s.get("stream_subtitle_decision") or s.get("subtitle_decision"), 32),
        "subtitle_codec": _text(s.get("subtitle_codec"), 32),
        "bitrate": _int(s.get("bitrate")),
        "stream_bitrate": _int(s.get("stream_bitrate")),
        "hw_decoding": _flag(s.get("transcode_hw_decoding")),
        "hw_encoding": _flag(s.get("transcode_hw_encoding")),
    }


def stream_data(config: TautulliConfig, history_id: int) -> dict[str, Any] | None:
    """how plex streamed a play, None when tautulli has nothing for it (it answers 400)"""
    try:
        data = _tautulli(config, "get_stream_data", row_id=history_id)
    except StatsError:
        return None
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            return None
        raise
    return data if isinstance(data, dict) and data else None


def _insert(engine: Engine, playbacks: list[dict[str, Any]]) -> None:
    if not playbacks:
        return
    with Session(engine) as session:
        session.execute(sqlite_insert(Playback).values(playbacks).on_conflict_do_nothing())
        session.commit()


def sync_playbacks(engine: Engine, config: TautulliConfig, now: datetime) -> int:
    """add new plays with their stream data and delete those older than KEEP. Reads from
    a day before the newest stored play (plays in progress have no id yet), or BACKFILL
    the first time. A finished play's stream doesn't change, so known plays aren't read
    again. Plays are stored oldest first: a sync that fails partway (tautulli timing out)
    leaves no gap, the next one carries on from the newest it stored. Returns the plays
    added"""
    with Session(engine) as session:
        session.execute(delete(Playback).where(Playback.started_at < now - KEEP))
        session.commit()
        newest = session.scalar(select(func.max(Playback.started_at)))
        since = now - BACKFILL if newest is None else newest - PLAYS_OVERLAP
        known = set(session.scalars(select(Playback.id).where(Playback.started_at >= since - PLAYS_OVERLAP)))
    params: dict[str, object] = {
        "grouping": 0,
        "order_column": "date",
        "order_dir": "desc",
        "length": TAUTULLI_PAGE,
        # tautulli compares dates, the started_at check below is exact
        "after": (since - timedelta(days=1)).date().isoformat(),
    }
    rows: list[Mapping[str, Any]] = []
    start = 0
    while True:
        data = _tautulli(config, "get_history", start=start, **params)
        page = history_rows(data)
        rows += page
        if len(page) < TAUTULLI_PAGE:
            break
        start += TAUTULLI_PAGE
    added = 0
    pending: list[dict[str, Any]] = []
    # tautulli pages newest first, so a play that starts while paging can't be skipped
    for row in reversed(rows):
        history_id = _int(row.get("id"))
        started_at = _utc(row.get("started") or row.get("date"))
        if history_id is None or history_id in known or started_at is None or started_at < since:
            continue
        if row.get("media_type") not in MEDIA_TYPES:
            continue
        # new plays shift the pages, so a row can come twice
        known.add(history_id)
        playback = playback_from(row, stream_data(config, history_id))
        if playback is None:
            continue
        pending.append(playback)
        added += 1
        if len(pending) >= COMMIT_EVERY:
            _insert(engine, pending)
            pending = []
    _insert(engine, pending)
    return added


def sync(engine: Engine, settings: ConfigSettings) -> SyncStatus:
    """one sync, its status saved. Never raises: the error is in the status"""
    status = SyncStatus(started_at=utc_now())
    try:
        if settings.tautulli is None:
            raise StatsError("Needs Tautulli on Settings → Connections")
        status.plays_read = sync_playbacks(engine, settings.tautulli, status.started_at)  # type: ignore[arg-type]
        if status.plays_read:
            logger.info("PLAYBACK\t Added %s plays", status.plays_read)
    except StatsError as e:
        status.error = str(e)
        logger.info("PLAYBACK\t Not synced: %s", e)
    except Exception as e:
        # requests errors hold urls, and tautulli's key is a query param
        status.error = redact(f"{type(e).__name__}: {e}", settings.tautulli.apikey if settings.tautulli else None)
        logger.error("PLAYBACK\t Sync failed: %s", status.error)
    status.finished_at = utc_now()
    save_status(engine, status, STATUS_SETTING)
    return status


class PlaybackSync(StatsSync):
    """syncs at start, every INTERVAL, and when sync_now() is called"""

    interval = INTERVAL
    status_key = STATUS_SETTING
    thread_name = "playback-sync"

    def sync(self, settings: ConfigSettings) -> SyncStatus:
        return sync(self.engine, settings)


# ---------------------------------------------------------------------------
# what it means
# ---------------------------------------------------------------------------


def _hdr(dynamic_range: str | None) -> bool:
    return bool(dynamic_range) and dynamic_range != "SDR"


def causes(p: Playback) -> list[str]:
    """why plex transcoded a play, the main one first; empty when it didn't. A quality
    limit transcodes the video whatever the file is, so the video's format only counts
    at Original quality. Audio only counts when the video wasn't transcoded"""
    if p.transcode_decision != "transcode":
        return []
    if not p.details:
        return ["unknown"]
    found = []
    video = p.video_decision == "transcode"
    if video and p.quality_profile and p.quality_profile != "Original":
        found.append("quality")
    if p.subtitle_decision == "burn":
        found.append("subtitles")
    if video and not found:
        if _hdr(p.video_dynamic_range) and not _hdr(p.stream_video_dynamic_range):
            found.append("hdr")
        elif p.video_codec and p.stream_video_codec and p.video_codec != p.stream_video_codec:
            found.append("video_codec")
        elif p.video_resolution and p.stream_video_resolution and p.video_resolution != p.stream_video_resolution:
            found.append("resolution")
    if not video and p.audio_decision == "transcode":
        found.append("audio")
    return found or ["unknown"]


# plex's codec names that aren't what people call them
CODEC_NAMES = {"dca": "DTS", "dca-ma": "DTS-HD MA", "truehd": "TrueHD", "hdmv_pgs_subtitle": "PGS", "mpeg2video": "MPEG-2"}


def _codec(codec: str | None) -> str:
    return CODEC_NAMES.get(codec or "", (codec or "").upper())


def _channels(channels: int | None) -> str:
    return {1: "mono", 2: "stereo", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}.get(channels or 0, f"{channels}ch" if channels else "")


def _change(before: str, after: str, changed: bool) -> str:
    if not changed:
        return before
    return f"{before} → {after}" if after and after != before else f"{before} re-encoded"


def _mbps(kbps: int) -> str:
    return f"{kbps / 1000:.1f}"


def detail(p: Playback) -> str:
    """how the play was streamed, e.g. 1080p H264 → 720p H264 · EAC3 5.1 → AAC · 4 Mbps 720p"""
    if not p.details:
        return "No stream details in Tautulli"
    parts = []
    if p.video_codec:
        video = " ".join(v for v in (p.video_resolution, _codec(p.video_codec)) if v)
        stream = " ".join(v for v in (p.stream_video_resolution, _codec(p.stream_video_codec)) if v)
        parts.append(_change(video, stream, p.video_decision == "transcode"))
        if _hdr(p.video_dynamic_range):
            parts.append(_change(p.video_dynamic_range or "", p.stream_video_dynamic_range or "", p.video_decision == "transcode"))
    if p.audio_codec:
        audio = " ".join(v for v in (_codec(p.audio_codec), _channels(p.audio_channels)) if v)
        stream = " ".join(v for v in (_codec(p.stream_audio_codec), _channels(p.stream_audio_channels)) if v)
        parts.append(_change(audio, stream, p.audio_decision == "transcode"))
    if p.subtitle_decision == "burn":
        parts.append(f"{_codec(p.subtitle_codec) or 'Subtitles'} burned in")
    if p.quality_profile and p.quality_profile != "Original":
        parts.append(p.quality_profile)
    if p.transcode_decision == "transcode" and p.bitrate and p.stream_bitrate:
        parts.append(f"{_mbps(p.bitrate)} → {_mbps(p.stream_bitrate)} Mbps")
    if p.video_decision == "transcode" and p.hw_encoding is not None:
        parts.append("hardware" if p.hw_encoding else "software (CPU)")
    return " · ".join(parts)


def file_format(p: Playback, cause: str) -> str:
    """the play's file in the terms of a file cause, e.g. EAC3 5.1, PGS (subtitles it
    showed), HEVC, HDR10; empty when it has none (no subtitles shown, SDR)"""
    match cause:
        case "audio":
            return " ".join(v for v in (_codec(p.audio_codec), _channels(p.audio_channels)) if v) if p.audio_codec else ""
        case "subtitles":
            return _codec(p.subtitle_codec)
        case "video_codec":
            return _codec(p.video_codec)
        case "hdr":
            return p.video_dynamic_range if _hdr(p.video_dynamic_range) else ""  # type: ignore[return-value]
    return ""


def player_name(p: Playback) -> str:
    return p.product or p.platform or "Unknown player"


def _percent(part: int, whole: int) -> int | None:
    return round(100 * part / whole) if whole else None


def _latest(p: Playback) -> dict[str, Any]:
    main = (causes(p) or [None])[0]
    return {
        "started_at": p.started_at,
        "decision": p.transcode_decision,
        "cause": CAUSES[main][0] if main else None,
        "title": p.title,
    }


def _group(plays: list[Playback]) -> dict[str, Any]:
    """counts for plays of one user or player, newest first"""
    decisions = Counter(p.transcode_decision for p in plays)
    main_causes = Counter(c[0] for p in plays if (c := causes(p)))
    # the quality limit it transcoded at most, what to change on the player
    limits = Counter(p.quality_profile for p in plays if causes(p)[:1] == ["quality"])
    # and the file format that made it transcode most, e.g. EAC3 5.1
    formats = Counter(
        file_format(p, c[0]) for p in plays if (c := causes(p)) and c[0] in FILE_CAUSES
    )
    transcodes = decisions["transcode"]
    return {
        "plays": len(plays),
        "direct_play": decisions["direct play"],
        "direct_stream": decisions["copy"],
        "transcode": transcodes,
        "transcode_percent": _percent(transcodes, len(plays)),
        "main_cause": CAUSES[main_causes.most_common(1)[0][0]][0] if main_causes else None,
        "quality_limit": limits.most_common(1)[0][0] if limits else None,
        "file_format": formats.most_common(1)[0][0] if formats else None,
        "latest": _latest(plays[0]),
    }


def fixes(plays: list[Playback]) -> dict[str, Any]:
    """the people's devices that cause most transcodes, worst first, and what to change on
    each: a setting lives on one app on one device, so by user and player together"""
    by_device: dict[tuple[int, str], list[Playback]] = defaultdict(list)
    for p in plays:
        by_device[p.user_id, player_name(p)].append(p)
    devices = []
    for (user_id, player), group in by_device.items():
        transcodes = [p for p in group if p.transcode_decision == "transcode"]
        if not transcodes:
            continue
        main = Counter(causes(p)[0] for p in transcodes).most_common(1)[0][0]
        of_main = [p for p in transcodes if causes(p)[0] == main]
        now = None
        if main == "quality":
            now = Counter(p.quality_profile for p in of_main).most_common(1)[0][0]
        elif main in FILE_CAUSES and (formats := Counter(f for p in of_main if (f := file_format(p, main)))):
            now = "can't play " + formats.most_common(1)[0][0]
        devices.append(
            {
                "user_id": user_id,
                "user": group[0].user,
                "player": player,
                "plays": len(group),
                "transcodes": len(transcodes),
                "cause": CAUSES[main][0],
                "now": now,
                "change": CHANGES[main],
            }
        )
    devices.sort(key=lambda d: (-d["transcodes"], -d["plays"]))
    total = sum(d["transcodes"] for d in devices)
    shown: list[dict[str, Any]] = []
    for d in devices:
        if len(shown) == FIXES or d["transcodes"] < FIX_MIN_TRANSCODES:
            break
        if sum(s["transcodes"] for s in shown) >= FIXES_COVER * total:
            break
        shown.append(d)
    rest = devices[len(shown):]
    return {
        "devices": shown,
        "percent": _percent(sum(d["transcodes"] for d in shown), total),
        "more": len(rest),
        "more_transcodes": sum(d["transcodes"] for d in rest),
    }


def report(plays: Iterable[Playback]) -> dict[str, Any]:
    """what the page shows, from plays newest first"""
    plays = list(plays)
    transcodes = [p for p in plays if p.transcode_decision == "transcode"]
    decisions = Counter(p.transcode_decision for p in plays)

    by_cause: dict[str, list[Playback]] = defaultdict(list)
    for p in transcodes:
        by_cause[causes(p)[0]].append(p)
    cause_rows = [
        {
            "cause": cause,
            "label": CAUSES[cause][0],
            "hint": CAUSES[cause][1],
            "plays": len(group),
            "percent": _percent(len(group), len(transcodes)),
            "users": len({p.user_id for p in group}),
        }
        for cause, group in sorted(by_cause.items(), key=lambda kv: (-len(kv[1]), list(CAUSES).index(kv[0])))
    ]

    by_user: dict[int, list[Playback]] = defaultdict(list)
    by_player: dict[str, list[Playback]] = defaultdict(list)
    for p in plays:
        by_user[p.user_id].append(p)
        by_player[player_name(p)].append(p)
    users = [
        {
            "user_id": user_id,
            "user": group[0].user,
            **_group(group),
            "players": sorted({player_name(p) for p in group}),
            "remote": sum(p.location == "wan" for p in group),
        }
        for user_id, group in by_user.items()
    ]
    players = [
        {
            "player": name,
            "platforms": sorted({p.platform for p in group if p.platform and p.platform != name}),
            **_group(group),
            "users": len({p.user_id for p in group}),
        }
        for name, group in by_player.items()
    ]

    def worst_first(row: dict[str, Any]) -> tuple[int, int]:
        return -row["transcode"], -row["plays"]

    # every play of a file format (e.g. EAC3 5.1 audio), and those it made transcode: a
    # high rate on many players is about the files, one player is about its user
    by_format: dict[tuple[str, str], list[Playback]] = defaultdict(list)
    for p in plays:
        for cause in FILE_CAUSES:
            if fmt := file_format(p, cause):
                by_format[cause, fmt].append(p)
    formats = []
    for (cause, fmt), group in by_format.items():
        issues = [p for p in group if causes(p)[:1] == [cause]]
        if not issues:
            continue
        formats.append(
            {
                "cause": CAUSES[cause][0],
                "format": fmt,
                "plays": len(group),
                "transcodes": len(issues),
                "percent": _percent(len(issues), len(group)),
                "users": len({p.user_id for p in issues}),
                "players": Counter(player_name(p) for p in issues).most_common(),
                "last_transcoded_at": issues[0].started_at,
            }
        )
    formats.sort(key=lambda f: (-f["transcodes"], -f["users"]))

    # the same file (rating key) transcoding for a file cause, for several people
    by_file: dict[int | str, list[Playback]] = defaultdict(list)
    for p in transcodes:
        if causes(p)[0] in FILE_CAUSES:
            by_file[p.rating_key if p.rating_key is not None else p.title].append(p)
    files = [
        {
            "title": group[0].title,
            "media_type": group[0].media_type,
            "causes": [CAUSES[c][0] for c in dict.fromkeys(causes(p)[0] for p in group)],
            "detail": detail(group[0]),
            "users": len({p.user_id for p in group}),
            "plays": len(group),
            "last_played_at": group[0].started_at,
        }
        for group in by_file.values()
        if len({p.user_id for p in group}) >= FILE_MIN_USERS
    ]
    files.sort(key=lambda f: (-f["users"], -f["plays"]))

    return {
        "plays": len(plays),
        "direct_play": decisions["direct play"],
        "direct_stream": decisions["copy"],
        "transcode": len(transcodes),
        "direct_play_percent": _percent(decisions["direct play"], len(plays)),
        "direct_stream_percent": _percent(decisions["copy"], len(plays)),
        "transcode_percent": _percent(len(transcodes), len(plays)),
        "remote": sum(p.location == "wan" for p in plays),
        "remote_percent": _percent(sum(p.location == "wan" for p in plays), len(plays)),
        "fixes": fixes(plays),
        # video transcodes the cpu encoded, plex's hardware transcoding wasn't used
        "software": sum(p.video_decision == "transcode" and p.hw_encoding is False for p in transcodes),
        "causes": cause_rows,
        "users": sorted(users, key=worst_first),
        "players": sorted(players, key=worst_first),
        "formats": formats[:FORMATS],
        "files": files[:FILES],
        "recent": [
            {
                "started_at": p.started_at,
                "user": p.user,
                "title": p.title,
                "player": player_name(p),
                "platform": p.platform,
                "remote": p.location == "wan",
                "causes": [CAUSES[c][0] for c in causes(p)],
                "detail": detail(p),
            }
            for p in transcodes[:RECENT]
        ],
    }


def window_plays(engine: Engine, since: datetime, user_id: int | None = None) -> list[Playback]:
    """plays since then, newest first"""
    query = select(Playback).where(Playback.started_at >= since)
    if user_id is not None:
        query = query.where(Playback.user_id == user_id)
    with Session(engine) as session:
        return list(session.scalars(query.order_by(Playback.started_at.desc(), Playback.id.desc())))
