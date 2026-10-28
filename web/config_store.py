"""config_store.py - the config, stored as versions of a document plus encrypted secrets.
config.toml is imported once, into the first version, and isn't read after that"""

import copy
import logging
import os
import pathlib
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

import config
from config.document import (
    DEFAULT_DOCUMENT,
    SECRET_FIELDS,
    canonical,
    from_toml,
    with_secrets,
)
from config.models import ConfigSettings, WebDefaults
from logging_config import hide_secrets
from web.db import ConfigSecret, ConfigVersion, RemoverRuleSetting, make_engine, migrate
from web.jobs import utc_now
from web.secret_box import SecretBox, SecretError
from web.settings import database_path, secret_key_problems

logger = logging.getLogger(__name__)

# the longest note a version keeps, the column's length
MAX_NOTE = 500


class ConfigConflict(Exception):
    """the config was saved by something else since the page was loaded"""


@dataclass(frozen=True)
class SecretInfo:
    updated_at: datetime
    updated_by: str | None
    # false when it can't be decrypted, e.g. HELPERR_SECRET_KEY changed
    readable: bool


def remover_key(rule: Mapping[str, Any], arr: str = "sonarr") -> str:
    """config.models.RemoverRule.key (MovieRemoverRule.key for radarr), from a rule's
    document form"""
    if arr == "radarr":
        return f"radarr:{rule.get('path', '').rstrip('/')}"
    return ",".join(sorted({t.casefold() for t in rule.get("tags", [])}))


def removers_of(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        remover_key(r, arr): r
        for arr in ("sonarr", "radarr")
        for r in document.get(arr, {}).get("removers", [])
    }


class ConfigStore:
    def __init__(self, engine: Engine, box: SecretBox):
        self.engine = engine
        self.box = box
        # a save checks the version it was based on, then adds the next one
        self._lock = threading.Lock()

    def current(self) -> ConfigVersion | None:
        with Session(self.engine) as session:
            return session.scalar(select(ConfigVersion).order_by(ConfigVersion.id.desc()))

    def document(self) -> tuple[int, dict[str, Any]]:
        """(the current version's id, 0 before any, and its document)"""
        version = self.current()
        if version is None:
            return 0, copy.deepcopy(DEFAULT_DOCUMENT)
        return version.id, copy.deepcopy(version.document)

    def secret_info(self) -> dict[str, SecretInfo]:
        with Session(self.engine) as session:
            stored = list(session.scalars(select(ConfigSecret)))
        info = {}
        for secret in stored:
            try:
                self.box.decrypt(secret.value)
                readable = True
            except SecretError:
                readable = False
            info[secret.name] = SecretInfo(secret.updated_at, secret.updated_by, readable)
        return info

    def secrets(self) -> dict[str, str]:
        """every secret decrypted, SecretError naming one that can't be"""
        with Session(self.engine) as session:
            stored = list(session.scalars(select(ConfigSecret)))
        secrets = {}
        for secret in stored:
            try:
                secrets[secret.name] = self.box.decrypt(secret.value)
            except SecretError as e:
                raise SecretError(
                    f"The saved {secret.name} {e}. Enter it again on Settings → Connections."
                ) from None
        return secrets

    def load_settings(self) -> ConfigSettings:
        """the config jobs run with"""
        _, document = self.document()
        secrets = self.secrets()
        # every log and stored run result leaves them out from here on
        hide_secrets(*secrets.values())
        return config.build_config(with_secrets(document, secrets))

    def web_defaults(self) -> WebDefaults:
        """the web app's defaults, without needing the secrets"""
        try:
            return config.build_config(self.document()[1]).web
        except ValueError:
            logger.exception("WEB\t Config invalid, using the default web settings")
            return WebDefaults()

    def save(
        self,
        document: dict[str, Any],
        by: str | None,
        note: str,
        expected_version: int | None,
        secrets: Mapping[str, str | None] | None = None,
    ) -> int:
        """validate and store a new version, with secrets to set (None deletes one).
        Raises ValueError when invalid, ConfigConflict when expected_version (0 for none
        yet, None to skip the check) isn't the current version. Auto-approve turns off
        for remover rules this changes or removes, they have to be reviewed again"""
        secrets = dict(secrets or {})
        unknown = secrets.keys() - SECRET_FIELDS.keys()
        if unknown:
            raise ValueError(f"unknown secrets {sorted(unknown)}")
        document = canonical(document)
        with self._lock, Session(self.engine) as session:
            latest = session.scalar(select(ConfigVersion).order_by(ConfigVersion.id.desc()))
            if expected_version is not None and (latest.id if latest else 0) != expected_version:
                raise ConfigConflict(
                    "The settings were changed since this page was loaded. Reload it and "
                    "make your change again."
                )
            # build with stand-ins for the secrets: their values never make it invalid
            stored = set(session.scalars(select(ConfigSecret.name)))
            names = (stored | {n for n, v in secrets.items() if v}) - {
                n for n, v in secrets.items() if not v
            }
            config.build_config(with_secrets(document, dict.fromkeys(names, "x")))

            now = utc_now()
            for name, value in secrets.items():
                secret = session.get(ConfigSecret, name)
                if value:
                    secret = secret or ConfigSecret(name=name)
                    secret.value = self.box.encrypt(value)
                    secret.updated_at = now
                    secret.updated_by = by
                    session.add(secret)
                elif secret is not None:
                    session.delete(secret)

            notes = [note]
            before = removers_of(latest.document) if latest else {}
            after = removers_of(document)
            for setting in session.scalars(
                select(RemoverRuleSetting).where(RemoverRuleSetting.auto_approve)
            ):
                key = setting.rule_key
                if key in after and before.get(key) == after[key]:
                    continue
                setting.auto_approve = False
                setting.updated_at = now
                setting.updated_by = by
                change = "removed" if key not in after else "changed"
                notes.append(f"auto-approve turned off for remover {key} ({change})")
                logger.warning("WEB\t Auto-approve off for remover %s, it was %s", key, change)

            version = ConfigVersion(
                document=document,
                note="; ".join(notes)[:MAX_NOTE],
                created_at=now,
                created_by=by,
            )
            session.add(version)
            session.commit()
            logger.info("WEB\t Config version %s saved by %s: %s", version.id, by, version.note)
            return version.id

    def history(self, limit: int = 100, before: int | None = None) -> list[ConfigVersion]:
        """newest first, those before a version id when paging back"""
        query = select(ConfigVersion).order_by(ConfigVersion.id.desc()).limit(limit)
        if before is not None:
            query = query.where(ConfigVersion.id < before)
        with Session(self.engine) as session:
            return list(session.scalars(query))

    def version(self, version_id: int) -> ConfigVersion | None:
        with Session(self.engine) as session:
            return session.get(ConfigVersion, version_id)

    def previous(self, version_id: int) -> ConfigVersion | None:
        with Session(self.engine) as session:
            return session.scalar(
                select(ConfigVersion)
                .where(ConfigVersion.id < version_id)
                .order_by(ConfigVersion.id.desc())
            )

    def import_toml(self, path: pathlib.Path) -> str | None:
        """import config.toml when there's no config yet. Returns why it failed, None when
        it was imported or there was nothing to import"""
        if self.current() is not None or not path.exists():
            return None
        try:
            document, secrets, notes = from_toml(config.read_toml(path))
            self.save(
                document,
                None,
                "; ".join([f"Imported from {path.name}", *notes]),
                0,
                secrets,
            )
        except Exception as e:  # shown on the settings page, the app still starts
            logger.exception("WEB\t Importing %s failed", path)
            return f"{type(e).__name__}: {e}"
        logger.info("WEB\t Imported %s, it isn't read again", path)
        return None


def open_store(env: Mapping[str, str] = os.environ) -> ConfigStore:
    """the store of $DATA_DIR/helperr.db for the command line, importing config.toml if
    the web app hasn't yet"""
    problems: list[str] = []
    secret_key = secret_key_problems(env, problems)
    if problems:
        raise ValueError("; ".join(problems))
    engine = make_engine(database_path(env))
    migrate(engine)
    store = ConfigStore(engine, SecretBox(secret_key))
    if error := store.import_toml(config.config_toml_path()):
        raise ValueError(f"config.toml couldn't be imported: {error}")
    return store
