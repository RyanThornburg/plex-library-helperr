"""db.py - sqlite database, schema changes go through alembic migrations in web/migrations"""

import pathlib
from datetime import datetime
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import JSON, Engine, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"


class Base(DeclarativeBase):
    pass


class JobRun(Base):
    """one run of a job, with its JobResult and captured log"""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(32))
    trigger: Mapped[str] = mapped_column(String(16))  # manual, schedule, api
    requested_by: Mapped[str | None] = mapped_column(String(320))
    # queued, running, succeeded, failed
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime]  # naive utc, like the rest of the app
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    log: Mapped[str | None] = mapped_column(Text)


def make_engine(path: pathlib.Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        # wal lets the ui read while a job writes; busy_timeout waits instead of failing
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def migrate(engine: Engine) -> None:
    """bring the database to the latest schema"""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    with engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
