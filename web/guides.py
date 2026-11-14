"""guides.py - the TRaSH Guides' json for trash_sync runs and Settings → TRaSH Guides,
kept in guide_snapshots: the guides are downloaded only when GitHub says their json
changed, and a plan is applied from the snapshot it was made from"""

import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

import trash_guides
from logging_config import failure_text, redacted
from trash_guides import GuideLoader, Guides
from web.db import AppSetting, GuideSnapshot
from results import utc_now

logger = logging.getLogger(__name__)

# snapshots kept: a plan is applied within [web] plan_max_age, the guides change weekly
KEEP = 5
STATUS_SETTING = "trash_guides"


def cached(engine: Engine, commit: str) -> Guides | None:
    with Session(engine) as session:
        snapshot = session.get(GuideSnapshot, commit)
        return None if snapshot is None else Guides.from_data(snapshot.commit, snapshot.data)


def newest(engine: Engine) -> Guides | None:
    """the newest stored snapshot, without asking GitHub (for the settings pages)"""
    with Session(engine) as session:
        snapshot = session.scalars(select(GuideSnapshot).order_by(GuideSnapshot.fetched_at.desc()).limit(1)).first()
        return None if snapshot is None else Guides.from_data(snapshot.commit, snapshot.data)


def _store(engine: Engine, commit: str, data: dict[str, Any]) -> None:
    with Session(engine) as session:
        session.merge(GuideSnapshot(commit=commit, fetched_at=utc_now(), data=data))
        session.commit()
        keep = session.scalars(select(GuideSnapshot.commit).order_by(GuideSnapshot.fetched_at.desc()).limit(KEEP)).all()
        session.execute(delete(GuideSnapshot).where(GuideSnapshot.commit.not_in(keep)))
        session.commit()


def status(engine: Engine) -> dict[str, Any]:
    """the last check: {checked_at, commit, error}"""
    with Session(engine) as session:
        setting = session.get(AppSetting, STATUS_SETTING)
    try:
        data = json.loads(setting.value) if setting else {}
    except ValueError:
        data = {}
    checked = data.get("checked_at")
    data["checked_at"] = datetime.fromisoformat(checked) if isinstance(checked, str) else None
    return data


def _save_status(engine: Engine, commit: str | None, error: str | None) -> None:
    with Session(engine) as session:
        setting = session.get(AppSetting, STATUS_SETTING) or AppSetting(key=STATUS_SETTING)
        setting.value = json.dumps(
            {"checked_at": utc_now().isoformat(), "commit": commit, "error": redacted(error) if error else None}
        )
        setting.updated_at = utc_now()
        session.add(setting)
        session.commit()


def load_guides(engine: Engine, commit: str | None = None) -> Guides:
    """the guides at a commit (a plan's), or the latest: GitHub is asked which commit
    that is, and the snapshot is downloaded only when it isn't stored yet. When GitHub
    can't be reached, the newest stored snapshot is used. Raises when there's none"""
    if commit is not None:
        if found := cached(engine, commit):
            return found
        logger.info("TRASH\t Downloading the guides at %s", commit[:12])
        data = trash_guides.download(commit)
        _store(engine, commit, data)
        return Guides.from_data(commit, data)
    try:
        latest = trash_guides.latest_commit()
    except Exception as e:
        error = f"Couldn't ask GitHub for the latest guides: {failure_text(e)}"
        _save_status(engine, None, error)
        if found := newest(engine):
            logger.warning("TRASH\t %s, using the stored ones from %s", error, found.commit[:12])
            return found
        raise RuntimeError(error) from None
    try:
        found = load_guides(engine, latest)
    except Exception as e:
        _save_status(engine, latest, f"Couldn't download the guides: {failure_text(e)}")
        raise
    _save_status(engine, latest, None)
    return found


def loader(engine: Engine) -> GuideLoader:
    return lambda commit: load_guides(engine, commit)
