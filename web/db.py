"""db.py - sqlite database, schema changes go through alembic migrations in web/migrations"""

import pathlib
from datetime import datetime
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    JSON,
    Engine,
    ForeignKey,
    String,
    Text,
    create_engine,
    event,
    false,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"


class Base(DeclarativeBase):
    pass


class AppSetting(Base):
    """a setting edited in the ui, by key (e.g. the webhook api key)"""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime]


class ConfigVersion(Base):
    """one saved version of the config document (config.toml's shape, no secrets), the
    newest is the config. Restoring an old version saves it again as a new one"""

    __tablename__ = "config_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON)
    # what changed, e.g. "Radarr mover 2 edited", shown in the history
    note: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime]
    created_by: Mapped[str | None] = mapped_column(String(320))


class ConfigSecret(Base):
    """an api key/token/password of the config, by config.document.SECRET_FIELDS name.
    Encrypted with HELPERR_SECRET_KEY, and never versioned"""

    __tablename__ = "config_secrets"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime]
    updated_by: Mapped[str | None] = mapped_column(String(320))


class Schedule(Base):
    """runs a job whenever its cron expression matches, in its own time zone"""

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    job: Mapped[str] = mapped_column(String(32))
    cron: Mapped[str] = mapped_column(String(100))
    # IANA name, so the cron doesn't depend on the container's TZ
    timezone: Mapped[str] = mapped_column(String(64), server_default="UTC")
    # like JobRun.dry_run: true forces dry run, false uses the config's dry_run settings
    dry_run: Mapped[bool] = mapped_column(server_default=true())
    enabled: Mapped[bool] = mapped_column(server_default=true())
    created_at: Mapped[datetime]
    # any edit counts as a fresh start, so a schedule never fires for times before it
    updated_at: Mapped[datetime]
    last_fired_at: Mapped[datetime | None]


class RemoverRuleSetting(Base):
    """web app settings for a remover rule of the config, keyed by RemoverRule.key.
    Editing or deleting a rule turns its auto-approve off (web.config_store)"""

    __tablename__ = "remover_rule_settings"

    rule_key: Mapped[str] = mapped_column(String(1000), primary_key=True)
    # delete this rule's removals without review, needs REMOVERS_ALLOW_APPLY
    auto_approve: Mapped[bool] = mapped_column(server_default=false())
    updated_at: Mapped[datetime]
    updated_by: Mapped[str | None] = mapped_column(String(320))


class JobRun(Base):
    """one run of a job, with its JobResult and captured log"""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(32))
    # manual, schedule, api, followup (empty trash after moves/removals/upgrades), webhook
    trigger: Mapped[str] = mapped_column(String(16))
    requested_by: Mapped[str | None] = mapped_column(String(320))
    # true when the run was asked to be dry, false means the config's dry_run settings applied
    dry_run: Mapped[bool] = mapped_column(server_default=false())
    # queued, running, succeeded, failed
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime]  # naive utc, like the rest of the app
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    log: Mapped[str | None] = mapped_column(Text)
    # the schedule that queued it, null for manual/api runs or a deleted schedule
    schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="SET NULL"), index=True
    )
    # joined: runs are shown after their session has closed
    schedule: Mapped[Schedule | None] = relationship(lazy="joined")
    # set on a run that applies a reviewed removers plan: the plan's run, and the
    # approved series/movies as [{"service", "media_id", "title", "file_ids"}]
    plan_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_runs.id", ondelete="SET NULL"), index=True
    )
    approved: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)


class TrashEvent(Base):
    """a radarr/sonarr webhook for a change that leaves files in plex's trash (an upgrade
    replaced a file), followed up with an empty trash like moves/removals (web.scheduler).
    Kept for WebDefaults.followup_lookback"""

    __tablename__ = "trash_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    received_at: Mapped[datetime] = mapped_column(index=True)  # naive utc
    # the webhook's eventType, e.g. Download
    event: Mapped[str] = mapped_column(String(32))
    # e.g. "Sonarr: Survivor (2000) S01E02", like JobRun.requested_by
    source: Mapped[str] = mapped_column(String(320))


class Play(Base):
    """a play from Tautulli's history, copied by web.stats so request stats don't read all
    of it every time. Matched to requests by plex guid, which survives a move to another
    library (a rating key doesn't), and a show's by its rating key too, which covers
    episodes plex no longer has"""

    __tablename__ = "plays"

    # tautulli's history id
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    started_at: Mapped[datetime] = mapped_column(index=True)  # naive utc
    stopped_at: Mapped[datetime | None]
    # plex account id, like a seerr user's plexId
    user_id: Mapped[int] = mapped_column(index=True)
    username: Mapped[str] = mapped_column(String(255))
    # movie, episode, track...
    media_type: Mapped[str] = mapped_column(String(16))
    rating_key: Mapped[int | None]
    # an episode's show
    grandparent_rating_key: Mapped[int | None] = mapped_column(index=True)
    season: Mapped[int | None]
    episode: Mapped[int | None]
    # plex://movie/..., plex://episode/... (local://... for unmatched files), or before
    # plex's new agents com.plexapp.agents.thetvdb://121361/6/1 (?lang= dropped)
    guid: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(500))
    percent_complete: Mapped[int]
    # reached tautulli's watched percent
    watched: Mapped[bool]
    play_duration: Mapped[int | None]  # seconds
    # direct play, copy, transcode
    transcode_decision: Mapped[str | None] = mapped_column(String(32))
    platform: Mapped[str | None] = mapped_column(String(64))
    product: Mapped[str | None] = mapped_column(String(64))
    player: Mapped[str | None] = mapped_column(String(255))


class MediaRequest(Base):
    """a seerr request with how much of it the requester watched, rewritten by every
    web.stats sync"""

    __tablename__ = "media_requests"

    # seerr's request id
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    # movie or tv
    media_type: Mapped[str] = mapped_column(String(8))
    tmdb_id: Mapped[int | None]
    tvdb_id: Mapped[int | None]
    title: Mapped[str] = mapped_column(String(500))
    year: Mapped[int | None]
    # the request's: pending, approved, declined, failed, completed
    status: Mapped[str] = mapped_column(String(16))
    # the media's: pending, processing, partially available, available, deleted...
    media_status: Mapped[str] = mapped_column(String(32))
    is_4k: Mapped[bool]
    # requested seasons, empty for a movie
    seasons: Mapped[list[int]] = mapped_column(JSON)
    requested_at: Mapped[datetime] = mapped_column(index=True)
    available_at: Mapped[datetime | None]
    # seerr's user id and display name
    requester_id: Mapped[int] = mapped_column(index=True)
    requester: Mapped[str] = mapped_column(String(255))
    # the requester's plex account (Play.user_id), None when no tautulli user matched
    requester_user_id: Mapped[int | None]
    # 1 for a movie in plex, or the episodes plex has of the requested seasons
    items: Mapped[int]
    # of those, how many the requester finished / played at all
    watched: Mapped[int]
    started: Mapped[int]
    # the requester's plays of it, including episodes plex no longer has
    requester_plays: Mapped[int]
    last_played_at: Mapped[datetime | None]
    # other people who played any of it
    other_viewers: Mapped[int]
    synced_at: Mapped[datetime]



class Playback(Base):
    """a play from the last few weeks of Tautulli's history with how plex streamed it
    (get_stream_data), for the playback page. web.playback deletes older ones: it's about
    what happens now, not what happened before players or files changed"""

    __tablename__ = "playbacks"

    # tautulli's history id
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    started_at: Mapped[datetime] = mapped_column(index=True)  # naive utc
    # plex account id and tautulli's friendly name
    user_id: Mapped[int] = mapped_column(index=True)
    user: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(16))
    rating_key: Mapped[int | None]
    title: Mapped[str] = mapped_column(String(500))
    platform: Mapped[str | None] = mapped_column(String(64))
    product: Mapped[str | None] = mapped_column(String(64))
    player: Mapped[str | None] = mapped_column(String(255))
    # lan or wan
    location: Mapped[str | None] = mapped_column(String(16))
    # direct play, copy (direct stream), transcode
    transcode_decision: Mapped[str | None] = mapped_column(String(32))
    # False when tautulli had no stream data for it, the columns below are then empty
    details: Mapped[bool]
    # the player's quality setting: Original, or a limit like "4 Mbps 720p"
    quality_profile: Mapped[str | None] = mapped_column(String(64))
    # the file's, then what was streamed
    container: Mapped[str | None] = mapped_column(String(32))
    stream_container: Mapped[str | None] = mapped_column(String(32))
    container_decision: Mapped[str | None] = mapped_column(String(32))
    video_decision: Mapped[str | None] = mapped_column(String(32))
    video_codec: Mapped[str | None] = mapped_column(String(32))
    stream_video_codec: Mapped[str | None] = mapped_column(String(32))
    video_resolution: Mapped[str | None] = mapped_column(String(16))
    stream_video_resolution: Mapped[str | None] = mapped_column(String(16))
    video_dynamic_range: Mapped[str | None] = mapped_column(String(32))
    stream_video_dynamic_range: Mapped[str | None] = mapped_column(String(32))
    audio_decision: Mapped[str | None] = mapped_column(String(32))
    audio_codec: Mapped[str | None] = mapped_column(String(32))
    stream_audio_codec: Mapped[str | None] = mapped_column(String(32))
    audio_channels: Mapped[int | None]
    stream_audio_channels: Mapped[int | None]
    # burn, copy, transcode
    subtitle_decision: Mapped[str | None] = mapped_column(String(32))
    subtitle_codec: Mapped[str | None] = mapped_column(String(32))
    # kbps
    bitrate: Mapped[int | None]
    stream_bitrate: Mapped[int | None]
    hw_decoding: Mapped[bool | None]
    hw_encoding: Mapped[bool | None]


class HealthIncident(Base):
    """a problem a service had, from when web.health's checks first saw it to when it
    cleared (ended_at None while it lasts). Kept for web.health.KEEP"""

    __tablename__ = "health_incidents"

    id: Mapped[int] = mapped_column(primary_key=True)
    # radarr, sonarr, sabnzbd, seerr, plex
    service: Mapped[str] = mapped_column(String(16), index=True)
    # the same problem from one check to the next: "down", "disk:/data", a health
    # message, "queue:<title>"...
    key: Mapped[str] = mapped_column(String(500))
    # the worst it was: warn or bad
    level: Mapped[str] = mapped_column(String(8))
    # the latest text
    text: Mapped[str] = mapped_column(String(1000))
    started_at: Mapped[datetime] = mapped_column(index=True)  # naive utc
    last_seen_at: Mapped[datetime]
    ended_at: Mapped[datetime | None] = mapped_column(index=True)

def make_engine(path: pathlib.Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        # wal lets the ui read while a job writes; busy_timeout waits instead of failing
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        # the container's filesystem is read-only, so temp files sqlite spills to (e.g.
        # the statement journal of a migration's DROP TABLE) would fail with disk I/O error
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.close()

    return engine


def migrate(engine: Engine) -> None:
    """bring the database to the latest schema"""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
