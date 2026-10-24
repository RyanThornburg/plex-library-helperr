"""Tests for the config document: config.toml import, secrets, canonical rules"""

from datetime import timedelta

import pytest

import config
from config.document import (
    canonical,
    from_toml,
    rule_to_dict,
    strip_secrets,
    with_secrets,
)
from config.models import MoverRule, ProfileRule, RemoverRule

TOML = {
    "settings": {"dry_run": False, "filter_time": "1d"},
    "plex": {"baseurl": "http://plex:32400", "token": "plex-token", "empty_trash": {"max_items": 10}},
    "tautulli": {"url": "http://tautulli:8181", "apikey": "tautulli-key"},
    "autoscan": {"url": "http://autoscan:3030", "username": "me", "password": "autoscan-pass"},
    "radarr": {
        "baseurl": "http://radarr:7878",
        "apikey": "radarr-key",
        "tag": "helperr",
        "movers": [{"path": "/movies/kids", "genres": ["Family"], "genre_match": "any", "exclude_paths": []}],
        "profilers": [{"profile": "NoDownload", "titles": ["Melania"], "monitored": False}],
    },
    "sonarr": {
        "baseurl": "http://sonarr:8989",
        "apikey": "sonarr-key",
        "remover_settings": {"dry_run": True, "max_deletes": 100},
        "removers": [{"type": "seasons", "keep": 2, "tags": ["rm-2"], "min_age": "90d", "include_specials": False}],
    },
}
SECRETS = {
    "radarr.apikey": "radarr-key",
    "sonarr.apikey": "sonarr-key",
    "plex.token": "plex-token",
    "tautulli.apikey": "tautulli-key",
    "autoscan.password": "autoscan-pass",
}


class TestFromToml:
    def test_secrets_are_split_out(self):
        document, secrets, notes = from_toml(TOML)
        assert secrets == SECRETS
        for value in SECRETS.values():
            assert value not in repr(document)
        assert notes == []

    def test_put_back_it_builds_the_same_config(self):
        document, secrets, _ = from_toml(TOML)
        settings = config.build_config(with_secrets(document, secrets))
        assert settings.radarr.apikey == "radarr-key"
        assert settings.plex.token == "plex-token"
        assert settings.autoscan.password == "autoscan-pass"
        assert settings.filter_time == timedelta(days=1)
        assert settings.radarr.movers[0].genres == ["Family"]
        assert settings.sonarr.remover_settings.max_deletes == 100

    def test_rules_are_canonical(self):
        document, _, _ = from_toml(TOML)
        # defaults and empty lists dropped, like a rule saved from the ui
        assert document["radarr"]["movers"] == [{"path": "/movies/kids", "genres": ["Family"]}]
        assert document["radarr"]["profilers"] == [
            {"profile": "NoDownload", "titles": ["Melania"], "monitored": False}
        ]
        assert document["sonarr"]["removers"] == [
            {"type": "seasons", "keep": 2, "tags": ["rm-2"], "min_age": "90d"}
        ]

    def test_remover_dry_run_is_dropped(self):
        document, _, notes = from_toml(TOML)
        assert document["sonarr"]["remover_settings"] == {"max_deletes": 100}
        assert notes == []  # it was on, nothing changes

    def test_missing_dry_run_stays_live(self):
        # config.toml defaulted to live, the document to dry
        document, _, _ = from_toml({})
        assert document["settings"] == {"dry_run": False}
        assert config.build_config({}).empty_trash.dry_run is True

    def test_live_removers_are_noted(self):
        data = {"sonarr": {"remover_settings": {"dry_run": False}}}
        document, _, notes = from_toml(data)
        assert document["sonarr"]["remover_settings"] == {}
        assert "dry_run = false was dropped" in notes[0]

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="radarr.movers"):
            from_toml({"radarr": {"movers": [{"path": "/m"}]}})

    def test_input_is_not_changed(self):
        before = repr(TOML)
        from_toml(TOML)
        assert repr(TOML) == before


class TestSecrets:
    def test_strip_and_put_back(self):
        document, secrets = strip_secrets(TOML)
        assert with_secrets(document, secrets) == TOML

    def test_a_secret_alone_configures_nothing(self):
        document = with_secrets({}, {"tautulli.apikey": "key"})
        assert document == {}
        assert config.build_config(document).tautulli is None


class TestCanonical:
    @pytest.mark.parametrize(
        "rule",
        [
            MoverRule(path="/m", genres=["a"], genre_match="all", exclude_tags=["x"], plex_library="Kids"),
            ProfileRule(profile="HD", networks=["HBO"]),
            RemoverRule(
                type="episodes",
                keep=5,
                tags=["rm"],
                min_age=timedelta(days=14),
                include_specials=True,
                skip_if_watched_within=timedelta(hours=36),
            ),
        ],
        ids=["mover", "profiler", "remover"],
    )
    def test_rule_round_trips(self, rule):
        data = rule_to_dict(rule)
        if isinstance(rule, RemoverRule):
            assert data["min_age"] == "14d"
            assert data["skip_if_watched_within"] == "36h"
            data = {
                **data,
                "min_age": config.parse_duration(data["min_age"], "x"),
                "skip_if_watched_within": config.parse_duration(data["skip_if_watched_within"], "x"),
            }
        assert type(rule)(**data) == rule

    def test_empty_rule_lists_are_dropped(self):
        assert canonical({"radarr": {"movers": [], "baseurl": "http://r"}}) == {
            "radarr": {"baseurl": "http://r"}
        }
