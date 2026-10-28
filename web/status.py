"""status.py - what radarr, sonarr, sabnzbd, seerr and plex (through tautulli) are doing
right now, for the status page. Read only, except ArrQueue, which removes stuck downloads
someone selected. Every service is asked at once, and one that is down or slow only marks
its own card"""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from config.models import ArrConfig, ConfigSettings, SabnzbdConfig, SeerrConfig, TautulliConfig
from web.lookups import redact

logger = logging.getLogger(__name__)

TIMEOUT = 10
# queue records read from radarr/sonarr, enough to find the stuck ones
ARR_QUEUE_PAGE = 200
# queue records read to clear stuck downloads: all of them, a season pack is one per episode
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
    "client": "Remove from the download client",
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
    """a row on a card: a download, a stuck import, a stream"""

    title: str
    detail: str = ""
    progress: int | None = None  # percent
    level: str = "ok"


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
    error: str | None = None

    def problem(self, level: str, text: str, key: str = "") -> None:
        self.problems.append(Problem(level, text, key))
        self.raise_state(level)

    def low_space(self, free_bytes: int, where: str, min_free_gb: int) -> None:
        """a problem when free space is under the [web] min_free_gb (0 is off)"""
        if min_free_gb and free_bytes < min_free_gb * 1024**3:
            self.problem("warn", f"Low disk space: {size(free_bytes)} free on {where}", f"disk:{where}")

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


def arr_queue(status: ServiceStatus, records: list[dict[str, Any]], total: int) -> None:
    """the queue's size and the records radarr/sonarr won't finish on their own"""
    downloading = 0
    for record in records:
        if record.get("trackedDownloadState") == "downloading":
            downloading += 1
        if level := stuck_level(record):
            status.items.append(Item(arr_title(record), "; ".join(stuck_messages(record)), level=level))
            status.raise_state(level)
    status.items_label = "Needs attention in the queue"
    queue = f"{total}"
    if downloading:
        queue += f", {downloading} downloading"
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
    queue = get("queue", pageSize=ARR_QUEUE_PAGE, **include)
    arr_queue(status, queue.get("records") or [], _int(queue.get("totalRecords")))
    missing = get("wanted/missing", pageSize=1, monitored="true").get("totalRecords")
    cutoff = get("wanted/cutoff", pageSize=1, monitored="true").get("totalRecords")
    status.facts.append(("Missing", f"{_int(missing)}"))
    status.facts.append(("Cutoff unmet", f"{_int(cutoff)}"))
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
    status.facts.append(("Queue", f"{total}"))
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
    status.items_label = "Downloading" if slots else ""
    for slot in slots:
        status.items.append(
            Item(
                str(slot.get("filename") or "Unknown"),
                " · ".join(
                    str(v)
                    for v in (slot.get("status"), slot.get("cat"), slot.get("timeleft"))
                    if v and v != "*"
                ),
                progress=_int(slot.get("percentage")),
            )
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
        reason = slot.get("fail_message")
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
        return _get_json(f"{base}/api/v1/{path}", "Seerr", headers={"X-Api-Key": key})

    about = get("status")
    status.version = about.get("version")
    if about.get("restartRequired"):
        status.problem("warn", "Seerr needs a restart to apply settings")
    if about.get("updateAvailable"):
        status.problem("info", "An update is available")
    counts = get("request/count")
    pending = _int(counts.get("pending"))
    if pending:
        status.problem("warn", f"{pending} request{'s' if pending != 1 else ''} waiting for approval", "pending")
    issues = _int(get("issue/count").get("open"))
    if issues:
        status.problem("warn", f"{issues} open issue{'s' if issues != 1 else ''}", "issues")
    status.facts += [
        ("Pending approval", f"{pending}"),
        # approved, not all of it in plex yet
        ("Processing", f"{_int(counts.get('processing'))}"),
        ("Available", f"{_int(counts.get('available'))}"),
        ("Open issues", f"{issues}"),
    ]
    return status


# ---------------------------------------------------------------------------
# plex, through tautulli
# ---------------------------------------------------------------------------


def stream(session: dict[str, Any]) -> Item:
    decision = str(session.get("transcode_decision") or "")
    quality = session.get("stream_video_full_resolution") or session.get("video_full_resolution")
    source = session.get("video_full_resolution")
    if quality and source and quality != source:
        quality = f"{source} → {quality}"
    player = session.get("player") or session.get("platform")
    product = session.get("product")
    return Item(
        f"{session.get('friendly_name') or session.get('user')}: {session.get('full_title') or session.get('title')}",
        " · ".join(
            str(v)
            for v in (
                decision.title() if decision else None,
                quality,
                f"{player} ({product})" if player and product else player or product,
                "paused" if session.get("state") == "paused" else None,
                "remote" if session.get("location") == "wan" else None,
            )
            if v
        ),
        progress=_int(session.get("progress_percent")),
        level="warn" if decision == "transcode" else "ok",
    )


def plex_status(tautulli: TautulliConfig | None) -> ServiceStatus:
    status = ServiceStatus("plex", "Plex")
    if tautulli is None:
        status.state = "off"
        return status
    base, key = tautulli.url.rstrip("/"), tautulli.apikey

    def cmd(name: str) -> Any:
        body = _get_json(f"{base}/api/v2", "Tautulli", params={"apikey": key, "cmd": name})
        response = body.get("response", {})
        if response.get("result") != "success":
            raise Refused(f"Tautulli refused: {response.get('message') or 'unknown error'}")
        return response.get("data") or {}

    if not cmd("server_status").get("connected"):
        status.problem("bad", "Tautulli can't reach the Plex server")
        return status
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
        status.facts.append(
            (
                "Bandwidth",
                f"{_int(activity.get('total_bandwidth')) / 1000:.1f} Mbps, "
                f"{_int(activity.get('wan_bandwidth')) / 1000:.1f} remote",
            )
        )
    status.items_label = "Now playing" if sessions else ""
    status.items = [stream(s) for s in sessions]
    return status


# ---------------------------------------------------------------------------


def failure(e: Exception, label: str, *secrets: str | None) -> str:
    """why a request to a service failed, secrets redacted"""
    if isinstance(e, Refused):
        return redact(str(e), *secrets)
    if isinstance(e, requests.exceptions.ConnectionError):
        return "Couldn't connect"
    if isinstance(e, requests.exceptions.Timeout):
        return f"No answer within {TIMEOUT}s"
    text = redact(f"{type(e).__name__}: {e}", *secrets)
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
        (ServiceStatus("plex", "Plex"), lambda: plex_status(settings.tautulli)),
        (ServiceStatus("radarr", "Radarr"), lambda: arr_status("radarr", "Radarr", settings.radarr, min_free)),
        (ServiceStatus("sonarr", "Sonarr"), lambda: arr_status("sonarr", "Sonarr", settings.sonarr, min_free)),
        (ServiceStatus("sabnzbd", "SABnzbd"), lambda: sabnzbd_status(settings.sabnzbd, now, min_free)),
        (ServiceStatus("seerr", "Seerr"), lambda: seerr_status(settings.seerr)),
    ]
    # every key is redacted from every card's error
    secrets = [
        getattr(service, "apikey", None)
        for service in (settings.radarr, settings.sonarr, settings.sabnzbd, settings.seerr, settings.tautulli)
        if service
    ]
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        futures = [pool.submit(_checked, status, check, *secrets) for status, check in checks]
        return [f.result() for f in futures]
