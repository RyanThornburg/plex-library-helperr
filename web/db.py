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
    # approved series as [{"series_id", "title", "file_ids"}]
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
