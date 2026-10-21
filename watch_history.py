"""watch_history.py"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from plexapi import exceptions as plexapi_exceptions

from config.models import PlexConfig, TautulliConfig
from plex import setup_plex_server

logger = logging.getLogger(__name__)

TAUTULLI_PAGE_SIZE = 1000

# new plex agents use "tvdb://123", legacy agents use "com.plexapp.agents.thetvdb://123?lang=en"
TVDB_GUID_RE = re.compile(r"(?:^tvdb|thetvdb)://(\d+)")


@dataclass(frozen=True)
class WatchEvent:
    """a single episode play, watched_at is naive utc to match the arr timestamps"""

    tvdb_id: int | None
    title: str
    season: int
    episode: int
    watched_at: datetime


def _tvdb_from_guids(guids: list[str]) -> int | None:
    for g in guids:
        match = TVDB_GUID_RE.search(g)
        if match:
            return int(match.group(1))
    return None


def _from_timestamp(ts: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).replace(tzinfo=None)  # type: ignore[arg-type]
    except TypeError, ValueError, OverflowError:
        return None


def _tautulli(config: TautulliConfig, cmd: str, **params: object) -> object:
    response = requests.get(
        f"{config.url.rstrip('/')}/api/v2",
        params={"apikey": config.apikey, "cmd": cmd, **params},
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()["response"]
    if body.get("result") != "success":
        raise ValueError(body.get("message") or f"tautulli {cmd} failed")
    return body["data"]


def _tautulli_show_tvdb_id(config: TautulliConfig, rating_key: object) -> int | None:
    data = _tautulli(config, "get_metadata", rating_key=rating_key)
    if not isinstance(data, dict) or not data:
        return None
    return _tvdb_from_guids([*data.get("guids", []), data.get("guid") or ""])


def get_tautulli_watches(
    config: TautulliConfig, since: datetime
) -> list[WatchEvent] | None:
    """episode plays (including partial) since a naive utc time, None if history is unavailable"""
    try:
        rows: list[dict[str, object]] = []
        start = 0
        while True:
            data = _tautulli(
                config,
                "get_history",
                media_type="episode",
                grouping=0,
                after=since.date().isoformat(),
                start=start,
                length=TAUTULLI_PAGE_SIZE,
            )
            page = data["data"] if isinstance(data, dict) else []
            rows.extend(page)
            if len(page) < TAUTULLI_PAGE_SIZE:
                break
            start += TAUTULLI_PAGE_SIZE

        tvdb_by_key: dict[object, int | None] = {}
        events: list[WatchEvent] = []
        for row in rows:
            watched_at = max(
                (
                    t
                    for t in (
                        _from_timestamp(row.get("stopped")),
                        _from_timestamp(row.get("date")),
                    )
                    if t
                ),
                default=None,
            )
            try:
                season = int(row["parent_media_index"])  # type: ignore[arg-type]
                episode = int(row["media_index"])  # type: ignore[arg-type]
            except KeyError, TypeError, ValueError:
                logger.debug("TAUTULLI\t Skipping history row without episode numbers")
                continue
            if watched_at is None or watched_at < since:
                continue

            key = row.get("grandparent_rating_key")
            if key not in tvdb_by_key:
                tvdb_by_key[key] = _tautulli_show_tvdb_id(config, key) if key else None
                if tvdb_by_key[key] is None:
                    logger.warning(
                        "TAUTULLI\t No tvdb id for '%s', matching by title",
                        row.get("grandparent_title"),
                    )
            events.append(
                WatchEvent(
                    tvdb_id=tvdb_by_key[key],
                    title=str(row.get("grandparent_title") or ""),
                    season=season,
                    episode=episode,
                    watched_at=watched_at,
                )
            )
    except (requests.exceptions.RequestException, KeyError, ValueError) as e:
        logger.error("TAUTULLI\t Failed to read watch history: %s", e)
        return None

    logger.info("TAUTULLI\t %s episode plays since %s", len(events), since)
    return events


def get_plex_watches(config: PlexConfig, since: datetime) -> list[WatchEvent] | None:
    """completed episode plays from plex history, partial plays are not recorded by plex"""
    plex = setup_plex_server(config)
    if not plex:
        return None

    try:
        # plex history and viewedAt use local time
        local_since = since.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        history = plex.history(mindate=local_since)
        tvdb_by_key: dict[int, int | None] = {}
        events: list[WatchEvent] = []
        for h in history:
            if getattr(h, "type", None) != "episode":
                continue
            key = h.grandparentRatingKey
            if h.parentIndex is None or h.index is None or h.viewedAt is None:
                continue
            if key not in tvdb_by_key:
                try:
                    show = plex.fetchItem(key)
                    tvdb_by_key[key] = _tvdb_from_guids(
                        [g.id for g in show.guids] + [show.guid or ""]
                    )
                except plexapi_exceptions.NotFound:
                    tvdb_by_key[key] = None
                if tvdb_by_key[key] is None:
                    logger.warning(
                        "PLEX\t No tvdb id for '%s', matching by title",
                        h.grandparentTitle,
                    )
            events.append(
                WatchEvent(
                    tvdb_id=tvdb_by_key[key],
                    title=h.grandparentTitle or "",
                    season=h.parentIndex,
                    episode=h.index,
                    watched_at=h.viewedAt.astimezone(timezone.utc).replace(tzinfo=None),
                )
            )
    except (plexapi_exceptions.PlexApiException, requests.exceptions.RequestException) as e:
        logger.error("PLEX\t Failed to read watch history: %s", e)
        return None

    logger.info("PLEX\t %s episode plays since %s", len(events), since)
    return events


def get_watch_history(
    tautulli: TautulliConfig | None, plex: PlexConfig | None, since: datetime
) -> list[WatchEvent] | None:
    """prefer tautulli (partial plays, all users), fall back to plex history"""
    if tautulli:
        return get_tautulli_watches(tautulli, since)
    if plex:
        logger.warning(
            "Tautulli not configured, using Plex history for watch protection (partial plays are not seen)"
        )
        return get_plex_watches(plex, since)
    logger.error("Watch protection needs [tautulli] or [plex] configured")
    return None
