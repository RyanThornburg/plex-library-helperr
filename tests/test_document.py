"""Tests for the config document: secrets, canonical rules"""

from datetime import timedelta

import pytest

import config
from config.document import (
    canonical,
    rule_to_dict,
    strip_secrets,
    with_secrets,
)
from config.models import MoverRule, MovieRemoverRule, ProfileRule, RemoverRule

DOCUMENT = {
    "settings": {"dry_run": False, "filter_time": "1d"},
    "plex": {"baseurl": "http://plex:32400", "token": "plex-token", "empty_trash": {"max_items": 10}},
    "tautulli": {"url": "http://tautulli:8181", "apikey": "tautulli-key"},
    "sabnzbd": {"url": "http://sabnzbd:8080", "apikey": "sabnzbd-key"},
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
        "remover_settings": {"max_deletes": 100},
        "removers": [{"type": "seasons", "keep": 2, "tags": ["rm-2"], "min_age": "90d", "include_specials": False}],
    },
}
SECRETS = {
    "radarr.apikey": "radarr-key",
    "sonarr.apikey": "sonarr-key",
    "plex.token": "plex-token",
    "tautulli.apikey": "tautulli-key",
    "sabnzbd.apikey": "sabnzbd-key",
    "autoscan.password": "autoscan-pass",
}


class TestSecrets:
    def test_secrets_are_split_out(self):
        document, secrets = strip_secrets(DOCUMENT)
        assert secrets == SECRETS
        for value in SECRETS.values():
            assert value not in repr(document)

    def test_put_back_it_builds_the_same_config(self):
        settings = config.build_config(with_secrets(*strip_secrets(DOCUMENT)))
        assert settings.radarr.apikey == "radarr-key"
        assert settings.plex.token == "plex-token"
        assert settings.autoscan.password == "autoscan-pass"
        assert settings.filter_time == timedelta(days=1)
        assert settings.radarr.movers[0].genres == ["Family"]
        assert settings.sonarr.remover_settings.max_deletes == 100

    def test_strip_and_put_back(self):
        document, secrets = strip_secrets(DOCUMENT)
        assert with_secrets(document, secrets) == DOCUMENT

    def test_a_secret_alone_configures_nothing(self):
        document = with_secrets({}, {"tautulli.apikey": "key"})
        assert document == {}
        assert config.build_config(document).tautulli is None


class TestCanonical:
    def test_rules_are_canonical(self):
        document = canonical(strip_secrets(DOCUMENT)[0])
        # defaults and empty lists dropped, like a rule saved from the ui
        assert document["radarr"]["movers"] == [{"path": "/movies/kids", "genres": ["Family"]}]
        assert document["radarr"]["profilers"] == [
            {"profile": "NoDownload", "titles": ["Melania"], "monitored": False}
        ]
        assert document["sonarr"]["removers"] == [
            {"type": "seasons", "keep": 2, "tags": ["rm-2"], "min_age": "90d"}
        ]

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="radarr.movers"):
            canonical({"radarr": {"movers": [{"path": "/m"}]}})

    def test_input_is_not_changed(self):
        before = repr(DOCUMENT)
        canonical(strip_secrets(DOCUMENT)[0])
        assert repr(DOCUMENT) == before

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

    def test_movie_remover_is_canonical(self):
        document = {
            "radarr": {
                "removers": [
                    {"path": "/movies/hallmark", "min_age": "17520h", "skip_if_ever_watched": False, "exclude_tags": []}
                ]
            }
        }
        assert canonical(document)["radarr"]["removers"] == [{"path": "/movies/hallmark", "min_age": "730d"}]
        rule = MovieRemoverRule(path="/m", min_age=timedelta(days=730), skip_if_ever_watched=True)
        assert rule_to_dict(rule) == {"path": "/m", "min_age": "730d", "skip_if_ever_watched": True}

    def test_empty_rule_lists_are_dropped(self):
        assert canonical({"radarr": {"movers": [], "baseurl": "http://r"}}) == {
            "radarr": {"baseurl": "http://r"}
        }
