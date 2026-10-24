"""Tests for the stored config: versions, encrypted secrets, the config.toml import"""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.models import WebDefaults
from web.config_store import ConfigConflict, ConfigStore, open_store
from web.db import ConfigSecret, RemoverRuleSetting, make_engine, migrate
from web.jobs import utc_now
from web.secret_box import SecretBox, SecretError

KEY = "k" * 40
RADARR = {"radarr": {"baseurl": "http://radarr:7878", "movers": [{"path": "/m", "genres": ["Family"]}]}}
REMOVER = {"type": "seasons", "keep": 2, "tags": ["rm-2"], "min_age": "90d"}


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(tmp_path / "helperr.db")
    migrate(engine)
    return engine


@pytest.fixture
def store(engine):
    return ConfigStore(engine, SecretBox(KEY))


class TestSecretBox:
    def test_round_trip(self):
        box = SecretBox(KEY)
        token = box.encrypt("radarr-key")
        assert "radarr-key" not in token
        assert box.decrypt(token) == "radarr-key"

    def test_another_key_cant_decrypt(self):
        token = SecretBox(KEY).encrypt("radarr-key")
        with pytest.raises(SecretError, match="HELPERR_SECRET_KEY"):
            SecretBox("o" * 40).decrypt(token)

    def test_short_key_refused(self):
        with pytest.raises(ValueError, match="at least 32"):
            SecretBox("short")


class TestStore:
    def test_starts_dry_with_no_versions(self, store):
        assert store.document() == (0, {"settings": {"dry_run": True}})
        assert store.load_settings().empty_trash.dry_run is True

    def test_save_adds_a_version(self, store):
        first = store.save(RADARR, "me@example.com", "Radarr mover 1 added", 0)
        assert store.document() == (first, RADARR)
        second = store.save({**RADARR, "settings": {"dry_run": True}}, None, "General settings", first)
        assert second > first
        assert [v.note for v in store.history()] == ["General settings", "Radarr mover 1 added"]
        assert store.previous(second).id == first

    def test_secrets_are_encrypted_at_rest(self, store, engine):
        store.save(RADARR, None, "Radarr connection", 0, {"radarr.apikey": "radarr-key"})
        with Session(engine) as session:
            [stored] = session.scalars(select(ConfigSecret))
        assert stored.name == "radarr.apikey"
        assert "radarr-key" not in stored.value
        assert store.load_settings().radarr.apikey == "radarr-key"
        assert "radarr-key" not in repr(store.history())

    def test_secret_deleted(self, store):
        version = store.save(RADARR, None, "a", 0, {"radarr.apikey": "radarr-key"})
        store.save(RADARR, None, "b", version, {"radarr.apikey": None})
        assert store.load_settings().radarr.apikey is None
        assert store.secret_info() == {}

    def test_stale_version_conflicts(self, store):
        store.save(RADARR, None, "a", 0)
        with pytest.raises(ConfigConflict):
            store.save({}, None, "b", 0)
        assert store.document()[1] == RADARR

    def test_invalid_is_refused(self, store):
        with pytest.raises(ValueError, match="filter_time"):
            store.save({"settings": {"filter_time": "soon"}}, None, "x", 0)
        assert store.current() is None

    def test_unknown_secret_refused(self, store):
        with pytest.raises(ValueError, match="unknown secrets"):
            store.save({}, None, "x", 0, {"radarr.password": "x"})

    def test_undecryptable_secret_is_reported(self, store, engine):
        store.save(RADARR, None, "a", 0, {"radarr.apikey": "radarr-key"})
        other = ConfigStore(engine, SecretBox("o" * 40))
        assert other.secret_info()["radarr.apikey"].readable is False
        with pytest.raises(SecretError, match="radarr.apikey .* Enter it again"):
            other.load_settings()

    def test_web_defaults(self, store):
        assert store.web_defaults() == WebDefaults()
        store.save({"web": {"plan_max_age": "3d"}}, None, "a", 0)
        assert store.web_defaults().plan_max_age == timedelta(days=3)


class TestAutoApproveReset:
    def turn_on(self, engine, key="rm-2"):
        with Session(engine) as session:
            session.add(RemoverRuleSetting(rule_key=key, auto_approve=True, updated_at=utc_now()))
            session.commit()

    def auto(self, engine, key="rm-2") -> bool:
        with Session(engine) as session:
            return session.get_one(RemoverRuleSetting, key).auto_approve

    def test_kept_when_the_rule_is_unchanged(self, store, engine):
        version = store.save({"sonarr": {"removers": [REMOVER]}}, None, "a", 0)
        self.turn_on(engine)
        store.save({"sonarr": {"removers": [REMOVER]}, "settings": {"dry_run": True}}, None, "b", version)
        assert self.auto(engine)

    @pytest.mark.parametrize(
        "removers, change",
        [([{**REMOVER, "keep": 1}], "changed"), ([], "removed")],
        ids=["edited", "deleted"],
    )
    def test_turned_off_when_the_rule_changes(self, store, engine, removers, change):
        version = store.save({"sonarr": {"removers": [REMOVER]}}, None, "a", 0)
        self.turn_on(engine)
        store.save({"sonarr": {"removers": removers}}, "me", "Sonarr remover 1 edited", version)
        assert not self.auto(engine)
        assert store.current().note == f"Sonarr remover 1 edited; auto-approve turned off for remover rm-2 ({change})"


class TestImport:
    def test_imports_once(self, store, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            "[settings]\ndry_run = false\n[radarr]\nbaseurl = 'http://radarr'\napikey = 'radarr-key'\n"
            "[sonarr.remover_settings]\ndry_run = false\n"
        )
        assert store.import_toml(path) is None
        version = store.current()
        assert version.note.startswith("Imported from config.toml; sonarr.remover_settings.dry_run = false was dropped")
        assert version.created_by is None
        assert store.load_settings().radarr.apikey == "radarr-key"
        assert "radarr-key" not in repr(version.document)
        # later starts leave the stored config alone
        path.write_text("[settings]\ndry_run = true\n")
        assert store.import_toml(path) is None
        assert store.current().id == version.id

    def test_invalid_toml_is_reported_and_nothing_stored(self, store, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("[settings]\nfilter_time = 'tomorrow'\n")
        assert "filter_time" in store.import_toml(path)
        assert store.current() is None

    def test_no_file_nothing_to_do(self, store, tmp_path):
        assert store.import_toml(tmp_path / "config.toml") is None
        assert store.current() is None


class TestOpenStore:
    def test_command_line_uses_the_same_store(self, tmp_path, monkeypatch):
        (tmp_path / "config.toml").write_text("[radarr]\nbaseurl = 'http://radarr'\napikey = 'radarr-key'\n")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        env = {"DATA_DIR": str(tmp_path), "HELPERR_SECRET_KEY": KEY}
        assert open_store(env).load_settings().radarr.apikey == "radarr-key"
        assert (tmp_path / "helperr.db").exists()

    def test_needs_the_secret_key(self, tmp_path):
        with pytest.raises(ValueError, match="HELPERR_SECRET_KEY"):
            open_store({"DATA_DIR": str(tmp_path)})
