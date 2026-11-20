"""status.py - what unraid, radarr, sonarr, sabnzbd, bazarr, seerr and plex (through tautulli, and
its own health from web.plex_health) are doing
right now, for the status page. Read only, except ArrQueue, which removes stuck downloads
someone selected. Every service is asked at once, and one that is down or slow only marks
its own card"""

import logging
import re
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from config.models import (
    ArrConfig,
    BazarrConfig,
    ConfigSettings,
    EmptyTrashSettings,
    PlexConfig,
    SabnzbdConfig,
    SeerrConfig,
    TautulliConfig,
    UnraidConfig,
)
from logging_config import failure_text, url_hosts
from tautulli import TautulliRefused, tautulli_get
from web.db import Playback
from web.lookups import BAZARR_NOT_API, redact
from web.playback import DECISIONS, stream_columns, tracks, transcode_reason
from web.plex_health import PlexRefused, check_plex
from web.seerr import SeerrRefused, seerr_get
from web.unraid import UnraidRefused, error_text, unraid_query

logger = logging.getLogger(__name__)

TIMEOUT = 10
# queue records read from radarr/sonarr: all of them, a season pack is a record per
# episode, so a few big packs would push stuck downloads past a smaller page
ARR_QUEUE_ALL = 2000
# sabnzbd downloads listed on the card
SAB_QUEUE_LIMIT = 10
# failed sabnzbd downloads within this long are a warning
SAB_FAILED_WITHIN = timedelta(days=7)
SAB_HISTORY_LIMIT = 50
# problems listed on a card, the rest are counted
MAX_PROBLEMS = 8

LEVELS = {"ok": 0, "info": 0, "warn": 1, "bad": 2}
# a queue record that needs someone to look at it: radarr/sonarr won't import it on
# their own, or the download failed
STUCK_STATES = {"importBlocked", "importPending", "failedPending", "failed"}
# how a stuck download leaves the queue, like Remove in the arrs' queue
REMOVAL_METHODS = {
    "client": "Remove from the download client too, deleting the download",
    "ignore": "Remove from the queue only, the download stays in the client",
}
BLOCKLIST = {
    "none": "Don't blocklist",
    "search": "Blocklist and search for a replacement",
    "only": "Blocklist only",
}


class Refused(Exception):
    """the service answered but refused, the message has no secrets in it"""


@dataclass
class Problem:
    level: str  # info, warn or bad
    text: str
    # the same problem from one check to the next, for its history (web.health), when
    # the text changes (a count, a countdown); empty means the text
    key: str = ""


@dataclass
class Item:
    """a row on a card: a download radarr/sonarr won't finish on their own"""

    title: str
    detail: str = ""
    level: str = "ok"
    # a queue download's id in the download client, for its incident ("" when it has none)
    key: str = ""
    # a stuck download's trackedDownloadStates, for queue cleanup
    states: list[str] = field(default_factory=list[str])


@dataclass
class Stream:
    """something plex is playing now (through tautulli's get_activity)"""

    user: str
    title: str
    # DECISIONS' words: Direct play, Direct stream, Transcode
    decision: str
    user_id: int | None = None
    # percent watched, and transcoded ahead of it (the buffer, transcodes only)
    progress: int = 0
    buffered: int | None = None
    # 38:12, 1:01:40
    position: str = ""
    duration: str = ""
    player: str = ""
    remote: bool = False
    paused: bool = False
    mbps: float | None = None
    # web.playback.tracks(): kind, file, sent, decision
    tracks: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    hardware: bool | None = None
    # why it transcodes and what to change (web.playback.transcode_reason)
    cause: str = ""
    now: str = ""
    change: str = ""


@dataclass
class Download:
    """a download in flight: one per download, from radarr/sonarr's queue and sabnzbd's,
    joined on the download id (activity())"""

    title: str
    # the release's name when the title is the movie or episode
    release: str = ""
    # radarr or sonarr when one of them grabbed it, else empty (sabnzbd's own)
    service: str = ""
    # downloading, queued, paused, importing, ... (DOWNLOAD_STATES' words)
    state: str = "downloading"
    progress: int | None = None
    # 23.2 GB of 38.1 GB, and 4m
    size: str = ""
    eta: str = ""
    # the download client's id: radarr/sonarr's downloadId, sabnzbd's nzo_id
    key: str = ""


@dataclass
class Meter:
    """a level drawn as a bar: a disk's fill, a parity check's progress. A live one is
    work happening now, shown with the downloads and streams"""

    label: str
    percent: int
    text: str = ""
    level: str = "ok"
    live: bool = False


@dataclass
class ServiceStatus:
    service: str
    label: str
    # ok, warn, bad, or off when it isn't connected
    state: str = "ok"
    version: str | None = None
    facts: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    problems: list[Problem] = field(default_factory=list[Problem])
    items: list[Item] = field(default_factory=list[Item])
    # what the items are, e.g. "Needs attention"
    items_label: str = ""
    # the folded line of a service that's fine, when its facts are too many for one line
    summary: str = ""
    error: str | None = None
    # what's happening now, for the status page's Right now band: plex's streams,
    # downloads, a parity check, and the band's heading for them (speed, bandwidth)
    streams: list[Stream] = field(default_factory=list[Stream])
    downloads: list[Download] = field(default_factory=list[Download])
    meters: list[Meter] = field(default_factory=list[Meter])
    activity_note: str = ""

    def problem(self, level: str, text: str, key: str = "") -> None:
        self.problems.append(Problem(level, text, key))
        self.raise_state(level)

    def low_space(self, free_bytes: int, where: str, min_free_gb: int) -> None:
        """a problem when free space is under the [web] min_free_gb (0 is off)"""
        if min_free_gb and free_bytes < min_free_gb * 1024**3:
            self.problem(
                "warn",
                f"Low disk space: {size(free_bytes)} free on {where}, under the {min_free_gb:,} GB minimum",
                f"disk:{where}",
            )

    def raise_state(self, level: str) -> None:
        if LEVELS.get(level, 0) > LEVELS.get(self.state, 0):
            self.state = level

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["facts"] = dict(self.facts)
        return data


def size(n: float | None) -> str:
    """bytes as 1.2 GB"""
    if n is None:
        return "-"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


def _int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except TypeError, ValueError:
        return default


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None


def eta(text: object) -> str:
    """radarr/sonarr's 1.02:03:04 or sabnzbd's 1:02:03:04 / 0:04:12 as 1d 2h, 4m"""
    try:
        parts = [int(p) for p in str(text).replace(".", ":").split(":")]
    except ValueError:
        return ""
    if not 1 <= len(parts) <= 4 or any(p < 0 for p in parts):
        return ""
    days, hours, minutes, seconds = [0] * (4 - len(parts)) + parts
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else ("under a minute" if seconds else "")


def clock(milliseconds: object) -> str:
    """a position in a video as 38:12 or 1:01:40"""
    seconds = _int(milliseconds) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _percent(part: float, whole: float) -> int:
    return max(0, min(100, round(100 * part / whole))) if whole else 0


def _get_json(url: str, label: str, **kwargs: Any) -> Any:
    response = requests.get(url, timeout=TIMEOUT, **kwargs)
    if response.status_code in (401, 403):
        raise Refused(f"{label} refused the API key (HTTP {response.status_code})")
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# radarr / sonarr
# ---------------------------------------------------------------------------


def arr_title(record: dict[str, Any]) -> str:
    """the movie or episode a queue record is for, else the release name"""
    if movie := record.get("movie"):
        year = movie.get("year")
        return f"{movie.get('title')} ({year})" if year else str(movie.get("title"))
    if series := record.get("series"):
        episode = record.get("episode") or {}
        season, number = episode.get("seasonNumber"), episode.get("episodeNumber")
        if season is not None and number is not None:
            return f"{series.get('title')} S{season:02d}E{number:02d}"
        return str(series.get("title"))
    return str(record.get("title") or "Unknown")


def stuck_level(record: dict[str, Any]) -> str | None:
    """warn or bad when radarr/sonarr won't finish the record on their own"""
    state = record.get("trackedDownloadState")
    health = record.get("trackedDownloadStatus")
    if state not in STUCK_STATES and health not in ("warning", "error"):
        return None
    return "bad" if health == "error" or state in ("failedPending", "failed") else "warn"


def stuck_messages(record: dict[str, Any]) -> list[str]:
    """why a record is stuck, from its messages, else its state"""
    messages = [
        m
        for s in record.get("statusMessages") or []
        for m in (s.get("messages") or [s.get("title")])
        if m
    ]
    if record.get("errorMessage"):
        messages.insert(0, record["errorMessage"])
    return list(dict.fromkeys(messages)) or [
        str(record.get("trackedDownloadState") or record.get("trackedDownloadStatus"))
    ]


# a queue record's status (or sabnzbd's, lower case) as the Right now band says it
DOWNLOAD_STATES = {
    "downloading": "downloading",
    "queued": "queued",
    "paused": "paused",
    "delay": "delayed",
    "completed": "downloaded",
    "downloadclientunavailable": "client unavailable",
    "grabbing": "grabbing",
    "fetching": "fetching",
    "propagating": "propagating",
    "checking": "checking",
}


def pack_title(records: list[dict[str, Any]]) -> str:
    """a download's movie or episodes: a season pack (a record per episode) as the
    series, its season and how many episodes"""
    if len(records) == 1 or not (series := records[0].get("series")):
        return arr_title(records[0])
    seasons = {(r.get("episode") or {}).get("seasonNumber") for r in records}
    season = next(iter(seasons)) if len(seasons) == 1 else None
    where = f" S{season:02d}" if isinstance(season, int) else ""
    return f"{series.get('title')}{where}, {len(records)} episodes"


def arr_download(service: str, records: list[dict[str, Any]]) -> Download:
    """one download from its queue records (a season pack has one per episode, each with
    the whole download's size)"""
    record = records[0]
    total, left = _float(record.get("size")) or 0, _float(record.get("sizeleft")) or 0
    title = pack_title(records)
    release = str(record.get("title") or "")
    if record.get("trackedDownloadState") == "importing":
        state = "importing"
    else:
        state = DOWNLOAD_STATES.get(str(record.get("status") or "").lower(), "downloading")
    return Download(
        title,
        release if release != title else "",
        service,
        state,
        progress=_percent(total - left, total) if total else None,
        size=f"{size(total - left)} of {size(total)}" if total else "",
        eta=eta(record.get("timeleft")) if state == "downloading" else "",
        key=str(record.get("downloadId") or ""),
    )


def arr_queue(status: ServiceStatus, records: list[dict[str, Any]], total: int) -> None:
    """the queue's size, its downloads in flight (the Right now band) and the downloads
    radarr/sonarr won't finish on their own. A season pack is a record per episode: it's
    one download here, as on the queue page"""
    downloading: set[object] = set()
    stuck: dict[object, tuple[Item, list[str], list[str]]] = {}
    flight: dict[object, list[dict[str, Any]]] = {}
    for index, record in enumerate(records):
        group = record.get("downloadId") or ("record", index)
        if record.get("trackedDownloadState") == "downloading":
            downloading.add(group)
        if not stuck_level(record):
            flight.setdefault(group, []).append(record)
        if level := stuck_level(record):
            item, titles, messages = stuck.setdefault(
                group, (Item("", level=level, key=str(record.get("downloadId") or "")), [], [])
            )
            if (title := arr_title(record)) not in titles:
                titles.append(title)
            if (state := str(record.get("trackedDownloadState") or "")) not in item.states:
                item.states.append(state)
            messages.extend(m for m in stuck_messages(record) if m not in messages)
            if LEVELS[level] > LEVELS[item.level]:
                item.level = level
    for item, titles, messages in stuck.values():
        item.title = titles[0] + (f" and {len(titles) - 1} more" if len(titles) > 1 else "")
        item.detail = "; ".join(messages)
        status.items.append(item)
        status.raise_state(item.level)
    # a download with a stuck episode is stuck: it's on the card, not in flight
    status.downloads = [arr_download(status.service, group) for key, group in flight.items() if key not in stuck]
    downloading_count = len(downloading)
    status.items_label = "Needs attention in the queue"
    queue = f"{total:,}"
    if downloading_count:
        queue += f", {downloading_count} downloading"
    if status.items:
        queue += f", {len(status.items)} need attention"
    status.facts.append(("Queue", queue))


@dataclass
class StuckDownload:
    """a download stuck in a radarr/sonarr queue. A season pack is a queue record per
    episode, removing any of them removes the download"""

    # the download client's id, else queue:<record id>
    key: str
    ids: list[int]
    # the movie, or each episode
    titles: list[str]
    release: str
    client: str
    detail: str
    level: str
    # each record's trackedDownloadState, once
    states: list[str] = field(default_factory=list[str])


def stuck_downloads(records: list[dict[str, Any]]) -> list[StuckDownload]:
    """the queue's stuck records, one per download in queue order"""
    downloads: dict[str, StuckDownload] = {}
    messages: dict[str, list[str]] = {}
    for record in records:
        level = stuck_level(record)
        if level is None or not isinstance(record.get("id"), int):
            continue
        key = str(record.get("downloadId") or f"queue:{record['id']}")
        title = arr_title(record)
        if download := downloads.get(key):
            download.ids.append(record["id"])
            if title not in download.titles:
                download.titles.append(title)
            if LEVELS[level] > LEVELS[download.level]:
                download.level = level
        else:
            downloads[key] = StuckDownload(
                key,
                [record["id"]],
                [title],
                str(record.get("title") or ""),
                str(record.get("downloadClient") or ""),
                "",
                level,
            )
        state = str(record.get("trackedDownloadState") or "")
        if state not in downloads[key].states:
            downloads[key].states.append(state)
        messages[key] = list(dict.fromkeys(messages.get(key, []) + stuck_messages(record)))
    for key, download in downloads.items():
        download.detail = "; ".join(messages[key])
    return list(downloads.values())


class ArrQueue:
    """radarr's or sonarr's queue, to remove what's stuck in it"""

    def __init__(self, service: str, label: str, arr: ArrConfig):
        self.service = service
        self.label = label
        self.base = arr.baseurl.rstrip("/")
        self.apikey = arr.apikey
        self.headers = {"X-Api-Key": arr.apikey}

    def stuck(self) -> list[StuckDownload]:
        include = (
            {"includeMovie": "true"}
            if self.service == "radarr"
            else {"includeSeries": "true", "includeEpisode": "true"}
        )
        queue = _get_json(
            f"{self.base}/api/v3/queue",
            self.label,
            headers=self.headers,
            params={"pageSize": ARR_QUEUE_ALL, **include},
        )
        return stuck_downloads(queue.get("records") or [])

    def health(self) -> list[dict[str, Any]]:
        """the arr's System → Status health checks"""
        return _get_json(f"{self.base}/api/v3/health", self.label, headers=self.headers)

    def remove(self, ids: list[int], method: str, blocklist: str) -> None:
        """removes the records' downloads. The arr looks every id up first and removes
        nothing when one is gone, so pass ids it just listed"""
        response = requests.delete(
            f"{self.base}/api/v3/queue/bulk",
            headers=self.headers,
            json={"ids": ids},
            params={
                "removeFromClient": str(method == "client").lower(),
                "blocklist": str(blocklist != "none").lower(),
                "skipRedownload": str(blocklist == "only").lower(),
            },
            timeout=TIMEOUT,
        )
        if response.status_code in (401, 403):
            raise Refused(f"{self.label} refused the API key (HTTP {response.status_code})")
        if response.status_code == 404:
            raise Refused(f"{self.label} no longer has one of them in its queue, nothing was removed")
        response.raise_for_status()


def clear_stuck(
    queue: ArrQueue, keys: list[str], method: str, blocklist: str
) -> tuple[list[StuckDownload], int]:
    """removes the selected downloads that are still stuck, from a fresh read of the
    queue. Returns them and how many selected ones weren't stuck any more"""
    if method not in REMOVAL_METHODS or blocklist not in BLOCKLIST:
        raise ValueError("Unknown removal method or blocklist option")
    keys = list(dict.fromkeys(keys))
    stuck = {d.key: d for d in queue.stuck()}
    chosen = [stuck[k] for k in keys if k in stuck]
    if chosen:
        queue.remove([i for d in chosen for i in d.ids], method, blocklist)
    return chosen, len(keys) - len(chosen)


def arr_status(service: str, label: str, arr: ArrConfig | None, min_free_gb: int = 0) -> ServiceStatus:
    status = ServiceStatus(service, label)
    if arr is None or not arr.baseurl or not arr.apikey:
        status.state = "off"
        return status
    base, key = arr.baseurl.rstrip("/"), arr.apikey

    def get(path: str, **params: Any) -> Any:
        return _get_json(
            f"{base}/api/v3/{path}", label, headers={"X-Api-Key": key}, params=params
        )

    status.version = get("system/status").get("version")
    for check in get("health"):
        level = {"error": "bad", "warning": "warn"}.get(check.get("type"), "info")
        status.problem(level, str(check.get("message") or check.get("source")))
    include = (
        {"includeMovie": "true"}
        if service == "radarr"
        else {"includeSeries": "true", "includeEpisode": "true"}
    )
    queue = get("queue", pageSize=ARR_QUEUE_ALL, **include)
    arr_queue(status, queue.get("records") or [], _int(queue.get("totalRecords")))
    missing = get("wanted/missing", pageSize=1, monitored="true").get("totalRecords")
    cutoff = get("wanted/cutoff", pageSize=1, monitored="true").get("totalRecords")
    status.facts.append(("Missing", f"{_int(missing):,}"))
    status.facts.append(("Cutoff unmet", f"{_int(cutoff):,}"))
    free: dict[int, list[str]] = {}
    for root in get("rootfolder"):
        if root.get("accessible") is False:
            status.problem("bad", f"Root folder {root.get('path')} isn't accessible")
        elif root.get("freeSpace") is not None:
            free.setdefault(_int(root["freeSpace"]), []).append(str(root.get("path")))
    # root folders on one disk show once, and without their paths when all are on it
    for space, paths in free.items():
        status.facts.append(("Free", size(space) if len(free) == 1 else f"{size(space)} ({', '.join(paths)})"))
        status.low_space(space, ", ".join(paths), min_free_gb)
    return status


# ---------------------------------------------------------------------------
# sabnzbd
# ---------------------------------------------------------------------------


def sab_warning(warning: object) -> str:
    # older versions list strings, newer ones {"text", "type", "time"}
    if isinstance(warning, dict):
        return str(warning.get("text") or "")
    return str(warning)


def sab_queue(status: ServiceStatus, queue: dict[str, Any], min_free_gb: int = 0) -> None:
    status.version = queue.get("version")
    paused = bool(queue.get("paused"))
    state = str(queue.get("status") or ("Paused" if paused else "Idle"))
    if paused:
        until = queue.get("pause_int")
        status.problem(
            "warn",
            f"Downloads are paused (resumes in {until})" if until and until != "0" else "Downloads are paused",
            "paused",
        )
    slots = queue.get("slots") or []
    total = _int(queue.get("noofslots_total"), len(slots))
    speed = _float(queue.get("kbpersec"))
    status.facts.append(("Status", state))
    status.facts.append(("Queue", f"{total:,}"))
    if total:
        status.facts.append(("Speed", f"{size(speed * 1024)}/s" if speed is not None else "-"))
        left = _float(queue.get("mbleft"))
        status.facts.append(
            ("Left", f"{size(left * 1024 * 1024) if left is not None else '-'}, {queue.get('timeleft') or '-'}")
        )
    for name, folder, key in (
        ("Free (incomplete)", "the incomplete folder", "diskspace1"),
        ("Free (complete)", "the complete folder", "diskspace2"),
    ):
        free = _float(queue.get(key))
        if free is not None:
            status.facts.append((name, size(free * 1024**3)))
            status.low_space(int(free * 1024**3), folder, min_free_gb)
    if total:
        done_in = eta(queue.get("timeleft"))
        note = (
            f"{size(speed * 1024)}/s" if speed is not None else "",
            f"{size(left * 1024 * 1024)} left" if left is not None else "",
            f"done in {done_in}" if done_in else "",
        )
        status.activity_note = "paused" if paused else " · ".join(v for v in note if v)
    status.downloads = [sab_download(slot) for slot in slots]


def sab_download(slot: dict[str, Any]) -> Download:
    total, left = _float(slot.get("mb")), _float(slot.get("mbleft"))
    state = DOWNLOAD_STATES.get(str(slot.get("status") or "").lower(), "downloading")
    return Download(
        str(slot.get("filename") or "Unknown"),
        state=state,
        progress=_int(slot.get("percentage")),
        size=f"{size((total - (left or 0)) * 1024**2)} of {size(total * 1024**2)}" if total else "",
        eta=eta(slot.get("timeleft")) if state == "downloading" else "",
        key=str(slot.get("nzo_id") or ""),
    )


def sab_failures(status: ServiceStatus, slots: list[dict[str, Any]], now: datetime) -> None:
    cutoff = now - SAB_FAILED_WITHIN
    recent = [
        s
        for s in slots
        if datetime.fromtimestamp(_int(s.get("completed")), timezone.utc).replace(tzinfo=None)
        >= cutoff
    ]
    for slot in recent:
        # a failed fetch names the nzb's url, with the indexer's key in it
        reason = url_hosts(str(slot.get("fail_message") or ""))
        status.problem(
            "warn",
            f"Failed: {slot.get('name')}" + (f" ({reason})" if reason else ""),
            f"failed:{slot.get('nzo_id') or slot.get('name')}",
        )
    status.facts.append((f"Failed, {SAB_FAILED_WITHIN.days} days", f"{len(recent)}"))


def sabnzbd_status(sab: SabnzbdConfig | None, now: datetime, min_free_gb: int = 0) -> ServiceStatus:
    status = ServiceStatus("sabnzbd", "SABnzbd")
    if sab is None:
        status.state = "off"
        return status
    base, key = sab.url.rstrip("/"), sab.apikey

    def api(mode: str, **params: Any) -> Any:
        # sabnzbd answers 200 with {"status": false, "error"} for some refusals
        body = _get_json(
            f"{base}/api",
            "SABnzbd",
            params={"mode": mode, "output": "json", "apikey": key, **params},
        )
        if isinstance(body, dict) and body.get("status") is False:
            raise Refused(f"SABnzbd refused: {body.get('error') or 'unknown error'}")
        return body

    sab_queue(status, api("queue", limit=SAB_QUEUE_LIMIT).get("queue") or {}, min_free_gb)
    history = api("history", limit=SAB_HISTORY_LIMIT, failed_only=1).get("history") or {}
    sab_failures(status, history.get("slots") or [], now)
    for warning in api("warnings").get("warnings") or []:
        if text := sab_warning(warning):
            status.problem("warn", text)
    return status


# ---------------------------------------------------------------------------
# seerr
# ---------------------------------------------------------------------------


def seerr_status(seerr: SeerrConfig | None) -> ServiceStatus:
    status = ServiceStatus("seerr", "Seerr")
    if seerr is None:
        status.state = "off"
        return status
    base, key = seerr.url.rstrip("/"), seerr.apikey

    def get(path: str) -> Any:
        return seerr_get(base, key, path, timeout=TIMEOUT)

    about = get("status")
    status.version = about.get("version")
    if about.get("restartRequired"):
        status.problem("warn", "Seerr needs a restart to apply settings")
    if about.get("updateAvailable"):
        status.problem("info", "An update is available")
    counts = get("request/count")
    pending = _int(counts.get("pending"))
    # requests waiting are work, not a fault: a note that doesn't make the card a warning
    if pending:
        status.problem("info", f"{pending:,} request{'s' if pending != 1 else ''} waiting for approval", "pending")
    issues = _int(get("issue/count").get("open"))
    if issues:
        status.problem("warn", f"{issues} open issue{'s' if issues != 1 else ''}", "issues")
    status.facts += [
        ("Pending approval", f"{pending:,}"),
        # approved, not all of it in plex yet
        ("Processing", f"{_int(counts.get('processing')):,}"),
        ("Available", f"{_int(counts.get('available')):,}"),
        ("Open issues", f"{issues:,}"),
    ]
    return status


# ---------------------------------------------------------------------------
# bazarr
# ---------------------------------------------------------------------------


def bazarr_answers(status: ServiceStatus, about: dict[str, Any], badges: dict[str, Any],
                   health: list[dict[str, Any]], providers: list[dict[str, Any]]) -> None:
    """bazarr's system/status, badges, system/health and providers on its card"""
    status.version = about.get("bazarr_version")
    for issue in health:
        where, text = issue.get("object"), str(issue.get("issue") or "unknown issue")
        status.problem("warn", f"{where}: {text}" if where else text, f"health:{where or text}")
    throttled = [p for p in providers if (p.get("status") or "Good") != "Good"]
    for provider in throttled:
        retry = provider.get("retry")
        status.problem(
            "warn",
            f"Provider {provider.get('name')} is throttled: {provider.get('status')}"
            + (f" (retries {retry})" if retry and retry != "-" else ""),
            f"provider:{provider.get('name')}",
        )
    # signalr is how bazarr hears about new episodes and movies right away, it's only
    # connected for the arrs bazarr uses (they have a version)
    live = []
    for arr, label in (("sonarr", "Sonarr"), ("radarr", "Radarr")):
        if not about.get(f"{arr}_version"):
            continue
        if badges.get(f"{arr}_signalr") == "DOWN":
            status.problem("warn", f"Not getting live updates from {label}, new media waits for the next sync", f"signalr:{arr}")
        else:
            live.append(label)
    episodes, movies = _int(badges.get("episodes")), _int(badges.get("movies"))
    status.facts.append(("Wanted", f"{episodes:,} episode, {movies:,} movie subtitles"))
    status.facts.append(("Providers", f"{len(providers)}, {len(throttled)} throttled" if throttled else f"{len(providers)}"))
    if live:
        status.facts.append(("Live updates", ", ".join(live)))


def bazarr_status(bazarr: BazarrConfig | None) -> ServiceStatus:
    status = ServiceStatus("bazarr", "Bazarr")
    if bazarr is None:
        status.state = "off"
        return status
    base, key = bazarr.url.rstrip("/"), bazarr.apikey

    def get(path: str) -> Any:
        try:
            body = _get_json(f"{base}/api/{path}", "Bazarr", headers={"X-API-KEY": key})
        except requests.exceptions.JSONDecodeError:
            raise Refused(BAZARR_NOT_API)
        return body.get("data", body) if isinstance(body, dict) else body

    bazarr_answers(status, get("system/status"), get("badges"), get("system/health"), get("providers"))
    return status


# ---------------------------------------------------------------------------
# unraid
# ---------------------------------------------------------------------------

UNRAID_DISK = "name status type rotational temp numErrors fsSize fsFree warning critical"
UNRAID_NOTIFICATION = "subject description"
# unread warnings and alerts read, of each
UNRAID_NOTIFICATIONS = 20
# everything the card shows, in one request. A field the key can't read (or docker while
# the array is stopped) answers null with an error, and the rest still answers
UNRAID_QUERY = f"""query HelperrStatus {{
  vars {{ version name }}
  info {{ display {{ unit warning critical hot max }} }}
  array {{
    state
    capacity {{ kilobytes {{ free total }} }}
    parityCheckStatus {{ status date duration speed errors progress running paused correcting }}
    parities {{ {UNRAID_DISK} }}
    disks {{ {UNRAID_DISK} }}
    caches {{ {UNRAID_DISK} }}
  }}
  notifications {{
    alerts: list(filter: {{type: UNREAD, importance: ALERT, offset: 0, limit: {UNRAID_NOTIFICATIONS}}}) {{ {UNRAID_NOTIFICATION} }}
    warnings: list(filter: {{type: UNREAD, importance: WARNING, offset: 0, limit: {UNRAID_NOTIFICATIONS}}}) {{ {UNRAID_NOTIFICATION} }}
  }}
  docker {{ containers {{ names state status autoStart }} }}
}}"""
# the UPS, as apcupsd reports it. Its own request: upsDevices can't be null, so its error
# (no UPS set up, apcupsd stopped) would null UNRAID_QUERY's whole answer
UPS_QUERY = """query HelperrUps {
  upsDevices { name status battery { chargeLevel estimatedRuntime } power { loadPercentage currentPower } }
}"""
# apcupsd's notifications all have this subject ("Power failure…", "Running on batteries",
# "Power has returned…"), and unraid never clears them: the UPS's live state replaces them
UPS_NOTICE = "UPS Alert"
# the array isn't started, in unraid's words (ArrayState)
ARRAY_STATES = {
    "STOPPED": "The array is stopped",
    "NEW_ARRAY": "The array is new and hasn't been started",
    "RECON_DISK": "The array is stopped: a disk is waiting to be rebuilt",
    "DISABLE_DISK": "The array is stopped with a disabled disk",
    "SWAP_DSBL": "The array is stopped: a disabled disk is being swapped",
    "INVALID_EXPANSION": "The array is stopped: too many disks were changed at once",
    "PARITY_NOT_BIGGEST": "The array is stopped: a parity disk is smaller than a data disk",
    "TOO_MANY_MISSING_DISKS": "The array is stopped: too many disks are missing",
    "NEW_DISK_TOO_SMALL": "The array is stopped: a new disk is too small for its slot",
    "NO_DATA_DISKS": "The array is stopped: it has no data disks",
}
# a disk that isn't fine (ArrayDiskStatus). DISK_OK is, and DISK_NP is an empty slot
DISK_STATES = {
    "DISK_NP_MISSING": ("bad", "{} is missing"),
    "DISK_DSBL": ("bad", "{} is disabled"),
    "DISK_NP_DSBL": ("bad", "{} is disabled and missing"),
    "DISK_WRONG": ("bad", "{} isn't the disk assigned to its slot"),
    "DISK_INVALID": ("warn", "{} is invalid: it's being rebuilt, or parity needs a sync"),
    "DISK_DSBL_NEW": ("warn", "{} is a replacement for a disabled disk, waiting to be rebuilt"),
    "DISK_NEW": ("warn", "{} is new and not part of the array yet"),
}
# what a disabled or missing disk means
DISK_GONE = ("DISK_NP_MISSING", "DISK_DSBL", "DISK_NP_DSBL")
DISK_EMULATED = {"DATA": ": Unraid is emulating it from parity", "PARITY": ": the array has less protection"}
# unraid's api gives sizes in decimal KB (it converts disks.ini's KiB)
UNRAID_KB = 1000
# unraid's own defaults for SSDs, which its api doesn't give (it gives the hard drives' ones)
SSD_HOT, SSD_MAX = 60, 70


def disk_label(name: str) -> str:
    """disk8 as Disk 8, parity2 as Parity 2, a pool by its name"""
    if found := re.fullmatch(r"(disk|parity)(\d*)", name):
        return f"{found[1].title()} {found[2]}".strip()
    return name


def _threshold(own: object, default: object) -> int:
    """a disk's own threshold, else the server's, 0 is off"""
    return _int(default if own is None else own)


def unraid_disk(status: ServiceStatus, disk: dict[str, Any], display: dict[str, Any]) -> None:
    """a disk's problems: its state, errors, temperature and how full it is, against
    the thresholds set in unraid"""
    name = str(disk.get("name") or "unnamed")
    label, kind = disk_label(name), disk.get("type")
    state = str(disk.get("status"))
    if found := DISK_STATES.get(state):
        level, text = found
        emulated = DISK_EMULATED.get(str(kind), "") if state in DISK_GONE else ""
        status.problem(level, text.format(label) + emulated, f"disk:{name}")
    errors = _int(disk.get("numErrors"))
    if errors:
        status.problem("warn", f"{label} has {errors:,} read/write error{'s' if errors != 1 else ''}", f"errors:{name}")
    temp = _float(disk.get("temp"))
    if temp is not None and temp == temp:  # NaN when it's spun down
        unit = "°F" if display.get("unit") == "FAHRENHEIT" else "°C"
        ssd = disk.get("rotational") is False
        hot, limit = (SSD_HOT, SSD_MAX) if ssd else (_int(display.get("hot")), _int(display.get("max")))
        if limit and temp >= limit:
            status.problem("bad", f"{label} is {temp:.0f}{unit}, over Unraid's {limit}{unit} limit", f"temp:{name}")
        elif hot and temp >= hot:
            status.problem("warn", f"{label} is running hot: {temp:.0f}{unit}, Unraid warns at {hot}{unit}", f"temp:{name}")
    total, free = _float(disk.get("fsSize")), _float(disk.get("fsFree"))
    if total and free is not None and kind != "PARITY":
        used = (total - free) / total * 100
        warning = _threshold(disk.get("warning"), display.get("warning"))
        critical = _threshold(disk.get("critical"), display.get("critical"))
        full = f"{label} is {used:.0f}% full, {size(free * UNRAID_KB)} free"
        level = "ok"
        if critical and used >= critical:
            level = "bad"
            status.problem("bad", f"{full} (critical at {critical}%)", f"space:{name}")
        elif warning and used >= warning:
            level = "warn"
            status.problem("warn", f"{full} (Unraid warns at {warning}%)", f"space:{name}")
        status.meters.append(Meter(label, _percent(total - free, total), f"{size(free * UNRAID_KB)} free", level))


def _hours(seconds: int) -> str:
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def unraid_parity(status: ServiceStatus, check: dict[str, Any]) -> None:
    """the parity check running now, else the last one's result"""
    state, errors = check.get("status"), _int(check.get("errors"))
    found = f"{errors:,} error{'s' if errors != 1 else ''}"
    if check.get("running") or state in ("RUNNING", "PAUSED"):
        paused = check.get("paused") or state == "PAUSED"
        what = "Correcting parity check" if check.get("correcting") else "Parity check"
        progress = f"{_int(check.get('progress'))}%"
        status.facts.append((what, ", ".join(str(v) for v in (progress, "paused" if paused else check.get("speed")) if v)))
        status.meters.append(
            Meter(
                what,
                max(0, min(100, _int(check.get("progress")))),
                ", ".join(str(v) for v in ("paused" if paused else check.get("speed"), found) if v),
                "warn" if errors else "ok",
                live=True,
            )
        )
        if errors:
            status.problem("warn", f"The parity check running now has found {found} so far", "parity")
        return
    result = {
        "COMPLETED": found if errors else "no errors",
        "CANCELLED": "cancelled",
        "FAILED": "failed",
        "NEVER_RUN": "never run",
    }.get(str(state), str(state).lower())
    took = _int(check.get("duration"))
    when = str(check.get("date") or "")[:10]
    status.facts.append(
        ("Last parity check", ", ".join(v for v in (when, result, f"took {_hours(took)}" if took else "") if v))
    )
    if state == "FAILED":
        status.problem("warn", "The last parity check failed", "parity")
    elif errors:
        status.problem("warn", f"The last parity check found {found}", "parity")


def unraid_containers(status: ServiceStatus, docker: dict[str, Any] | None, errors: list[dict[str, Any]]) -> None:
    """containers set to start with the array that aren't running"""
    containers = (docker or {}).get("containers")
    if containers is None:
        status.problem("warn", f"Docker containers can't be read: {error_text(errors, 'docker')}", "docker")
        return
    running = sum(1 for c in containers if c.get("state") == "RUNNING")
    for container in containers:
        if not container.get("autoStart") or container.get("state") == "RUNNING":
            continue
        name = str((container.get("names") or ["unnamed"])[0]).lstrip("/")
        detail = container.get("status") or str(container.get("state") or "").lower()
        status.problem(
            "warn",
            f"Container {name} isn't running" + (f" ({detail})" if detail else "") + ", but it's set to autostart",
            f"container:{name}",
        )
    stopped = len(containers) - running
    status.facts.append(("Containers", f"{running} running" + (f", {stopped} stopped" if stopped else "")))


def ups_reading(unraid: UnraidConfig) -> dict[str, Any] | None:
    """the UPS's live state, or None when there's none to read: no UPS set up, apcupsd
    stopped, or a key that can't read it. Never downs the card, unraid's own UPS
    notifications are shown instead"""
    try:
        data, _ = unraid_query(unraid.url, unraid.apikey, UPS_QUERY, timeout=TIMEOUT)
    except UnraidRefused, requests.RequestException, ValueError:
        return None
    devices = data.get("upsDevices")
    if isinstance(devices, list) and devices and isinstance(devices[0], dict):
        return devices[0]
    return None


def unraid_ups(status: ServiceStatus, ups: dict[str, Any]) -> bool:
    """the UPS as a fact, and a problem when it isn't on mains power or isn't well. apcupsd's
    STATUS is words: ONLINE, ONBATT, LOWBATT, REPLBATT, NOBATT, OVERLOAD, COMMLOST, SHUTTING
    DOWN, several at once. True when unraid has contact with it, so its state is live"""
    text = str(ups.get("status") or "").strip()
    flags = set(text.upper().split())
    battery, power = ups.get("battery") or {}, ups.get("power") or {}
    charge, load = _int(battery.get("chargeLevel")), _int(power.get("loadPercentage"))
    left = _hours(_int(battery.get("estimatedRuntime")))
    if "COMMLOST" in flags:
        # what apcupsd still says is from before it lost contact
        status.facts.append(("UPS", "No contact"))
        status.problem("warn", "Unraid has lost contact with the UPS", "ups:contact")
        return False
    if "SHUTTING" in flags:
        status.problem("bad", "The UPS is shutting the server down", "ups:shutdown")
    if "ONBATT" in flags:
        low = ", and the battery is low" if "LOWBATT" in flags else ""
        status.problem("bad", f"The UPS is running on battery: {charge}% charged, about {left} left{low}", "ups:battery")
    elif "LOWBATT" in flags:
        status.problem("warn", f"The UPS battery is low: {charge}% charged", "ups:low")
    if "NOBATT" in flags:
        status.problem("bad", "The UPS has no battery, or it's disconnected", "ups:nobattery")
    if "OVERLOAD" in flags:
        status.problem("bad", f"The UPS is overloaded: {load}% load", "ups:overload")
    if "REPLBATT" in flags:
        status.problem("warn", "The UPS says its battery needs replacing", "ups:replace")
    if not flags & {"ONLINE", "ONBATT", "SHUTTING"}:
        status.problem("warn", f"The UPS reports {text or 'no status'}", "ups:status")
    state = "On battery" if "ONBATT" in flags else "Online" if "ONLINE" in flags else text.title() or "Unknown"
    watts = _float(power.get("currentPower"))
    status.facts.append(
        ("UPS", f"{state}, {charge}% charged, {left} runtime, {load}% load" + (f" ({watts:.0f} W)" if watts else ""))
    )
    return True


def unraid_notifications(
    status: ServiceStatus, notes: dict[str, Any] | None, errors: list[dict[str, Any]], ups_live: bool = False
) -> None:
    """unraid's unread alerts and warnings (smart warnings among them), until they're
    dismissed in unraid. apcupsd's are left out while the UPS's live state is on the card"""
    if notes is None:
        status.problem("warn", f"Unraid's notifications can't be read: {error_text(errors, 'notifications')}", "notifications")
        return
    for level, kind in (("bad", "alerts"), ("warn", "warnings")):
        for note in notes.get(kind) or []:
            subject = str(note.get("subject") or "").strip()
            if ups_live and subject == UPS_NOTICE:
                continue
            description = str(note.get("description") or "").strip()
            text = f"{subject}: {description}" if subject and description and description != subject else subject or description
            # from plugins too: a url in one might hold a key that isn't ours
            status.problem(level, url_hosts(f"Notification: {text}"), f"notice:{subject}|{description}")


def unraid_answers(
    status: ServiceStatus, data: dict[str, Any], errors: list[dict[str, Any]], ups: dict[str, Any] | None = None
) -> None:
    """unraid's answer to UNRAID_QUERY (and the UPS's to UPS_QUERY, None without one) on
    its card, the worst problems first"""
    about = data.get("vars") or {}
    status.version = about.get("version")
    display = (data.get("info") or {}).get("display") or {}
    array = data.get("array")
    if not array:
        raise Refused(f"Unraid didn't say how the array is: {error_text(errors, 'array')}")
    state = array.get("state")
    started = state == "STARTED"
    if not started:
        status.problem("bad", ARRAY_STATES.get(str(state), f"The array isn't started ({state})"), "array")
    parities, disks, pools = (
        [d for d in array.get(kind) or [] if isinstance(d, dict) and d.get("status") != "DISK_NP"]
        for kind in ("parities", "disks", "caches")
    )
    for disk in parities + disks + pools:
        unraid_disk(status, disk, display)
    if about.get("name"):
        status.facts.append(("Server", str(about["name"])))
    kilobytes = (array.get("capacity") or {}).get("kilobytes") or {}
    free, total = _float(kilobytes.get("free")), _float(kilobytes.get("total"))
    space = f"{size(free * UNRAID_KB)} free of {size(total * UNRAID_KB)}" if free is not None and total else ""
    status.facts.append(("Array", ("Started" if started else "Stopped") + (f", {space}" if space else "")))
    status.facts.append(("Disks", f"{len(disks)} data, {len(parities)} parity"))
    temps = [(t, d) for d in parities + disks + pools if (t := _float(d.get("temp"))) is not None and t == t]
    if temps:
        hottest, disk = max(temps, key=lambda found: found[0])
        unit = "°F" if display.get("unit") == "FAHRENHEIT" else "°C"
        status.facts.append(("Hottest disk", f"{disk_label(str(disk.get('name')))}, {hottest:.0f}{unit}"))
    # a pool with more than one device has its size on the first
    if pool_space := [
        f"{p.get('name')} {size(pool_free * UNRAID_KB)} free"
        for p in pools
        if _float(p.get("fsSize")) and (pool_free := _float(p.get("fsFree"))) is not None
    ]:
        status.facts.append(("Pools", ", ".join(pool_space)))
    unraid_parity(status, array.get("parityCheckStatus") or {})
    # folded: how much room is left, and a parity check while one runs
    checking = [f"{label} {value}" for label, value in status.facts if label in ("Parity check", "Correcting parity check")]
    status.summary = " · ".join([space or ("Array started" if started else "Array stopped"), *checking])
    ups_live = unraid_ups(status, ups) if ups is not None else False
    unraid_notifications(status, data.get("notifications"), errors, ups_live)
    # docker stops with the array
    if started:
        unraid_containers(status, data.get("docker"), errors)
    status.problems.sort(key=lambda p: -LEVELS.get(p.level, 0))


def unraid_status(unraid: UnraidConfig | None) -> ServiceStatus:
    status = ServiceStatus("unraid", "Unraid")
    if unraid is None:
        status.state = "off"
        return status
    data, errors = unraid_query(unraid.url, unraid.apikey, UNRAID_QUERY, timeout=TIMEOUT)
    unraid_answers(status, data, errors, ups_reading(unraid))
    return status


# ---------------------------------------------------------------------------
# plex: streams through tautulli, its own health from web.plex_health
# ---------------------------------------------------------------------------


def stream_title(session: dict[str, Any]) -> str:
    """Severance S02E07 · Chikhai Bardo, Dune: Part Two (2024)"""
    title = str(session.get("title") or "")
    if session.get("media_type") == "episode" and (show := session.get("grandparent_title")):
        season, episode = session.get("parent_media_index"), session.get("media_index")
        try:
            where = f" S{int(season):02d}E{int(episode):02d}"
        except TypeError, ValueError:
            where = ""
        return f"{show}{where}" + (f" · {title}" if title else "")
    if session.get("media_type") == "movie" and session.get("year"):
        return f"{title} ({session['year']})"
    return str(session.get("full_title") or title or "Unknown")


def stream(session: dict[str, Any]) -> Stream:
    decision = str(session.get("transcode_decision") or "")
    play = Playback(transcode_decision=decision, details=True, **stream_columns(session))
    player = session.get("player") or session.get("platform")
    product = session.get("product")
    bandwidth = _float(session.get("bandwidth"))
    found = Stream(
        str(session.get("friendly_name") or session.get("user") or "Someone"),
        stream_title(session),
        DECISIONS.get(decision, decision.title()),
        user_id=_int(session.get("user_id"), 0) or None,
        progress=max(0, min(100, _int(session.get("progress_percent")))),
        position=clock(session.get("view_offset")) if session.get("view_offset") is not None else "",
        duration=clock(session.get("duration")) if _int(session.get("duration")) else "",
        player=f"{player} ({product})" if player and product else str(player or product or ""),
        remote=session.get("location") == "wan",
        paused=session.get("state") == "paused",
        mbps=bandwidth / 1000 if bandwidth is not None else None,
        tracks=tracks(play),
        hardware=play.hw_encoding if play.video_decision == "transcode" else None,
    )
    if decision == "transcode":
        # how far ahead plex has transcoded, when it's ahead of where they are
        buffered = _int(session.get("transcode_progress"), -1)
        found.buffered = buffered if buffered > found.progress else None
        if reason := transcode_reason(play):
            found.cause, found.now, found.change = reason["cause"], reason["now"], reason["change"]
    return found


def plex_status(
    tautulli: TautulliConfig | None, plex: PlexConfig | None = None, max_trash: int = EmptyTrashSettings.max_items
) -> ServiceStatus:
    """what's playing through Tautulli, then what Plex says itself (web.plex_health) when
    its token is set: libraries' trash and files, its settings, CPU transcodes"""
    status = ServiceStatus("plex", "Plex")
    direct = plex if plex is not None and plex.baseurl and plex.token else None
    if tautulli is None and direct is None:
        status.state = "off"
        return status
    if tautulli is not None and not tautulli_activity(status, tautulli):
        return status
    if direct is not None:
        try:
            check_plex(status, direct.baseurl, direct.token, max_trash)  # type: ignore[arg-type]
        except Exception as e:
            # without Tautulli there's nothing else on the card: it's down
            if tautulli is None:
                raise
            status.problem("warn", f"Helperr can't check Plex's trash, files and settings: {failure(e, 'Plex', direct.token)}", "direct")
    status.problems.sort(key=lambda p: -LEVELS.get(p.level, 0))
    return status


def tautulli_activity(status: ServiceStatus, tautulli: TautulliConfig) -> bool:
    """streams and bandwidth onto the card, False when Tautulli can't reach Plex"""
    base, key = tautulli.url.rstrip("/"), tautulli.apikey

    def cmd(name: str) -> Any:
        return tautulli_get(base, key, name, timeout=TIMEOUT) or {}

    if not cmd("server_status").get("connected"):
        status.problem("bad", "Tautulli can't reach the Plex server")
        return False
    status.version = cmd("get_server_info").get("pms_version")
    activity = cmd("get_activity")
    sessions = activity.get("sessions") or []
    streams = _int(activity.get("stream_count"), len(sessions))
    kinds = ", ".join(
        f"{count} {kind}"
        for kind, key in (
            ("direct play", "stream_count_direct_play"),
            ("direct stream", "stream_count_direct_stream"),
            ("transcode", "stream_count_transcode"),
        )
        if (count := _int(activity.get(key)))
    )
    status.facts.append(("Streams", f"{streams}: {kinds}" if kinds else f"{streams}"))
    if streams:
        # tautulli's bandwidth is in kbps
        bandwidth = (
            f"{_int(activity.get('total_bandwidth')) / 1000:.1f} Mbps, "
            f"{_int(activity.get('wan_bandwidth')) / 1000:.1f} remote"
        )
        status.facts.append(("Bandwidth", bandwidth))
        status.activity_note = bandwidth
    status.streams = [stream(s) for s in sessions]
    return True


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# right now: what's playing and downloading, across the services
# ---------------------------------------------------------------------------

# the downloads the band lists, the rest are counted
ACTIVITY_DOWNLOADS = 6
# nearly done first, then what's moving, then what's waiting
STATE_ORDER = {"importing": 0, "downloading": 1}


@dataclass
class Activity:
    """the status page's Right now band"""

    streams: list[Stream] = field(default_factory=list[Stream])
    # plex's bandwidth, sabnzbd's speed, left and time
    plex_note: str = ""
    downloads: list[Download] = field(default_factory=list[Download])
    more: int = 0
    download_note: str = ""
    # (service, label, how many) stuck downloads, on their cards rather than here
    stuck: list[tuple[str, str, int]] = field(default_factory=list[tuple[str, str, int]])
    # (service label, meter): a parity check running
    meters: list[tuple[str, Meter]] = field(default_factory=list[tuple[str, Meter]])
    # one line for the verdict: 4 streaming, 1 transcode · 3 downloading
    summary: str = ""

    def __bool__(self) -> bool:
        return bool(self.streams or self.downloads or self.meters)


def activity(services: list[ServiceStatus]) -> Activity:
    """what's playing and downloading now. A download is in radarr's or sonarr's queue
    and in sabnzbd's: it's one row, the arr's (its title is the movie or episodes) with
    sabnzbd's progress, joined on the download id. A download stuck in an arr's queue is
    on its card, not here"""
    found = {s.service: s for s in services if s.state != "off" and not s.error}
    plex, sab = found.get("plex"), found.get("sabnzbd")
    downloads: list[Download] = []
    by_key: dict[str, int] = {}
    stuck_keys: set[str] = set()
    stuck = []
    for service in ("radarr", "sonarr"):
        if (arr := found.get(service)) is None:
            continue
        stuck_items = [i for i in arr.items if i.level != "ok"]
        stuck_keys.update(i.key for i in stuck_items if i.key)
        if stuck_items:
            stuck.append((service, arr.label, len(stuck_items)))
        for download in arr.downloads:
            if download.key:
                by_key[download.key] = len(downloads)
            downloads.append(replace(download))
    for slot in sab.downloads if sab else []:
        if slot.key and slot.key in stuck_keys:
            continue
        if slot.key in by_key:
            own = downloads[by_key[slot.key]]
            # the client knows best how far a download has got; an arr importing it is past that
            if own.state != "importing":
                own.state, own.progress, own.eta = slot.state, slot.progress, slot.eta
                own.size = slot.size or own.size
        else:
            downloads.append(replace(slot))
    downloads.sort(key=lambda d: STATE_ORDER.get(d.state, 2))
    meters = [(s.label, m) for s in services if not s.error for m in s.meters if m.live]
    streams = plex.streams if plex else []
    transcodes = sum(1 for s in streams if s.decision == DECISIONS["transcode"])
    moving = Counter(d.state for d in downloads)
    summary = [
        f"{len(streams)} streaming" + (f", {transcodes} transcoding" if transcodes else "") if streams else "",
        ", ".join(f"{moving[state]} {state}" for state in ("downloading", "importing") if moving[state])
        or (f"{len(downloads)} queued" if downloads else ""),
        *(f"{m.label.lower()} {m.percent}%" for _, m in meters),
    ]
    return Activity(
        streams=streams,
        plex_note=plex.activity_note if plex else "",
        downloads=downloads[:ACTIVITY_DOWNLOADS],
        more=max(0, len(downloads) - ACTIVITY_DOWNLOADS),
        download_note=sab.activity_note if sab and downloads else "",
        stuck=stuck,
        meters=meters,
        summary=" · ".join(p for p in summary if p),
    )


def failure(e: Exception, label: str, *secrets: str | None) -> str:
    """why a request to a service failed, secrets redacted"""
    if isinstance(e, (Refused, TautulliRefused, SeerrRefused, UnraidRefused, PlexRefused)):
        return redact(str(e), *secrets)
    if isinstance(e, requests.exceptions.ConnectionError):
        return "Couldn't connect"
    if isinstance(e, requests.exceptions.Timeout):
        return f"No answer within {TIMEOUT}s"
    if isinstance(e, requests.exceptions.JSONDecodeError):
        return "Answered with a web page, not its API: check its URL on Settings → Connections, including any base URL"
    if isinstance(e, (AttributeError, KeyError, TypeError)):
        # the service answered, but not what this version of helperr expects
        logger.exception("WEB\t %s: unexpected answer", label)
        return "Unexpected answer: it may be a version Helperr doesn't know yet (see the web log)"
    text = redact(f"{type(e).__name__}: {failure_text(e)}", *secrets)
    logger.warning("WEB\t %s failed: %s", label, text)
    return text


def _checked(status: ServiceStatus, check: Callable[[], ServiceStatus], *secrets: str | None) -> ServiceStatus:
    """the check's status, or the card marked bad with why (secrets redacted)"""
    try:
        return check()
    except Exception as e:
        status.error = failure(e, f"Status of {status.label}", *secrets)
    status.state = "bad"
    return status


def check_all(settings: ConfigSettings, now: datetime) -> list[ServiceStatus]:
    """every service's status, in the page's order"""
    min_free = settings.web.min_free_gb
    checks: list[tuple[ServiceStatus, Callable[[], ServiceStatus]]] = [
        # the server under everything else first: a disk dropping out is the worst news
        (ServiceStatus("unraid", "Unraid"), lambda: unraid_status(settings.unraid)),
        (ServiceStatus("plex", "Plex"), lambda: plex_status(settings.tautulli, settings.plex, settings.empty_trash.max_items)),
        (ServiceStatus("radarr", "Radarr"), lambda: arr_status("radarr", "Radarr", settings.radarr, min_free)),
        (ServiceStatus("sonarr", "Sonarr"), lambda: arr_status("sonarr", "Sonarr", settings.sonarr, min_free)),
        (ServiceStatus("bazarr", "Bazarr"), lambda: bazarr_status(settings.bazarr)),
        (ServiceStatus("sabnzbd", "SABnzbd"), lambda: sabnzbd_status(settings.sabnzbd, now, min_free)),
        (ServiceStatus("seerr", "Seerr"), lambda: seerr_status(settings.seerr)),
    ]
    # every key is redacted from every card's error
    secrets = [
        getattr(service, "apikey", None)
        for service in (settings.radarr, settings.sonarr, settings.sabnzbd, settings.seerr, settings.tautulli, settings.bazarr, settings.unraid)
        if service
    ] + [settings.plex.token if settings.plex else None]
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        futures = [pool.submit(_checked, status, check, *secrets) for status, check in checks]
        return [f.result() for f in futures]
